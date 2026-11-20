# -*- coding: utf-8 -*-
"""
law_go_kr.py — 국가법령정보센터 Open API(DRF) 클라이언트
nts-tax-mcp 확장 모듈: 대법원 판례 · 법령(현행+연혁) · 법령해석례

- 인증: 환경변수 LAW_API_OC(law.go.kr 가입 시 발급받는 기관코드) 필수. 등록된 IP에서만 동작.
- 응답: XML을 경량 파싱 (JSON 지원이 target마다 들쭉날쭉해 XML로 통일).
- 연혁 워크플로우(2026-07-21 확립): lawSearch target=eflaw로 시행본 목록
  → 특정 시행본 원문은 lawService target=law&MST=… → 조문은 <조문번호> 위치 기준 순차 슬라이스.
"""
import difflib
import os
import re
import html
import time
import requests

# https로 호출한다 — 같은 OC·같은 서버 IP에서 https+JSON을 쓰는 Law MCP는 정상인데
# http 호출만 결과 0건이 나는 사례가 있었다 (2026-11-20, Render). 공식 가이드 URL도 https.
BASE = os.environ.get("LAW_API_BASE", "https://www.law.go.kr/DRF")
# 환경변수에 붙여넣을 때 앞뒤 공백·줄바꿈·따옴표가 섞이면 인증이 조용히 실패한다
_OC_RAW = os.environ.get("LAW_API_OC", "")
# str.strip()은 제로폭 공백(U+200B)·BOM(U+FEFF)·전각 공백 등을 못 지운다. OC는 영문·숫자로만
# 발급되므로 허용 문자만 남긴다. 걸러낸 문자는 오류 진단에 표시한다.
OC = re.sub(r"[^A-Za-z0-9._@-]", "", _OC_RAW)
_OC_DROPPED = sorted({f"U+{ord(c):04X}" for c in _OC_RAW if not re.match(r"[A-Za-z0-9._@-]", c)})
TIMEOUT = 20

# 같은 법령의 조문을 연속 조회하면 매번 법 전체 XML(소득세법 61만 자 등)을
# 다시 받게 되므로, 응답을 짧게 캐싱한다. 법령 개정은 실시간이 아니므로 안전.
_CACHE_TTL = 600   # 초
_CACHE_MAX = 16    # 전문 XML이 커서(수십만 자) 항목 수로 메모리 상한
_cache = {}        # key -> (만료시각, 응답텍스트)

_TAG_RE = re.compile(r"<([^/>\s]+)>\s*(.*?)\s*</\1>", re.S)


class LawGoKrError(Exception):
    """입력 오류 등 일반 오류 (호출자가 INVALID_INPUT으로 분류)"""


class LawGoKrAuthError(LawGoKrError):
    """OC 미설정·IP 미등록 등 인증 실패 — 자료 부존재와 무관"""


class LawGoKrNotFound(LawGoKrError):
    """검색·조회는 성공했으나 해당 자료가 원천에 없음"""


class LawGoKrParseError(LawGoKrError):
    """응답은 받았으나 예상한 XML 구조가 아님 (원천 개편 가능성) — 자료 부존재와 무관"""


# 별표·서식 파일 링크는 상대경로("/LSW/flDownload.do?flSeq=…")로 온다 —
# 이 호스트를 앞에 붙여야 HWP·PDF 원본을 받을 수 있다 (open.law.go.kr 활용방법 안내)
FILE_HOST = "https://www.law.go.kr"


def _abs_link(path: str) -> str:
    path = (path or "").strip()
    if not path:
        return ""
    return path if path.startswith("http") else FILE_HOST + (path if path.startswith("/") else "/" + path)


def _korean_date_to_ymd(text: str) -> str:
    """'2027년 1월 1일' → '20270101'. 못 찾으면 빈 문자열."""
    m = re.search(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", text or "")
    if not m:
        return ""
    return f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"


def _article_label(base: str, branch: str = "") -> str:
    branch = (branch or "").lstrip("0")
    return f"제{base}조" + (f"의{branch}" if branch else "")


def _strip_cdata(s: str) -> str:
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S)
    return html.unescape(s).strip()


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


def _strip_html(s: str) -> str:
    """HTML 태그(영문 태그명)만 지운다. 별표·자치법규 본문의 '<개정 2026. 2. 27.>',
    '<신설 2020. 5. 27.>' 같은 한글 꺾쇠 표기는 태그가 아니라 내용이므로 남긴다
    (_strip_tags는 이것까지 지워 개정일 정보가 사라졌다 — 2026-11-20 실응답 확인)."""
    return re.sub(r"</?[A-Za-z][^>]*>", "", s)


_AMEND_RE = re.compile(r"<\s*((?:개정|신설|전문개정|제정)\s*[^<>]*?)\s*>")


def _amend_note(text: str) -> str:
    """본문 머리의 '<개정 2026. 2. 27.>' 표기 → '개정 2026. 2. 27.' (없으면 빈 문자열)."""
    m = _AMEND_RE.search(text or "")
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


# Law MCP(lawApi.ts)와 같게 Referer·Origin을 채운다 — 같은 OC·같은 서버 IP에서 Law MCP만 정상이고
# Tax MCP는 "필수입력요소 검증에 실패"가 났는데, 진단상 OC·요청주소·리다이렉트는 모두 정상이었다
# (2026-11-20). 남은 차이가 이 헤더와 type(JSON/XML)뿐이었다.
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/xml, text/xml, */*",
    "Referer": "https://www.law.go.kr/",
    "Origin": "https://www.law.go.kr",
}


def _probe_json(endpoint: str, ordered) -> str:
    """XML 요청이 거부됐을 때 같은 요청을 type=JSON으로 한 번 보내 결과만 진단에 적는다."""
    try:
        alt = [(k, "JSON" if k == "type" else v) for k, v in ordered]
        r = requests.get(f"{BASE}/{endpoint}", params=alt, timeout=TIMEOUT,
                         headers={**_HEADERS, "Accept": "application/json, */*"})
        t = r.text.lstrip()
        ok = t.startswith("{") and "검증에 실패" not in t[:500]
        msg = re.sub(r"\s+", " ", _strip_tags(t))[:120]
        return "성공(JSON은 허용됨)" if ok else f"실패: {msg}"
    except Exception as e:
        return f"확인 불가({type(e).__name__})"


def _mask_oc(url: str) -> str:
    return re.sub(r"(OC=)([^&]*)", lambda m: m.group(1) + (m.group(2)[:3] + "***" if m.group(2) else "(비어 있음)"), url)


def _diag(r) -> str:
    """오류 메시지에 붙일 진단 정보 — 실제 요청 주소(OC 가림)·리다이렉트·OC 상태."""
    parts = []
    try:  # 진단은 오류 보고를 돕는 부가정보 — 여기서 예외가 나 원래 오류를 가리면 안 된다
        req = getattr(r, "request", None)
        parts.append(f"요청주소={_mask_oc(getattr(req, 'url', '') or '(알 수 없음)')}")
        history = getattr(r, "history", None) or []
        if history:
            hops = " → ".join(f"{h.status_code} {_mask_oc(h.headers.get('Location', ''))}" for h in history)
            parts.append(f"리다이렉트={hops}; 최종주소={_mask_oc(getattr(r, 'url', ''))}")
        else:
            parts.append("리다이렉트=없음")
        parts.append(f"HTTP={getattr(r, 'status_code', '?')}")
    except Exception as e:
        parts.append(f"요청정보 확인 실패({type(e).__name__})")
    parts.append(f"OC길이(원본/정리후)={len(_OC_RAW)}/{len(OC)}")
    parts.append(f"OC제거문자={','.join(_OC_DROPPED) if _OC_DROPPED else '없음'}")
    parts.append(f"BASE={BASE}")
    return " | ".join(parts)


def _check_response(endpoint: str, text: str, diag: str = "") -> None:
    """정상 결과가 아닌 응답(미신청·검증 실패 안내문, HTML 오류 페이지 등)을 걸러낸다.
    이걸 놓치면 파서가 항목 0건으로 읽어 NOT_FOUND(부존재)로 보고하는 사고가 난다.
    - 목록(lawSearch.do) 정상 응답에는 0건이어도 <totalCnt>가 반드시 있다
    - 본문(lawService.do) 정상 응답은 XML이며 HTML 페이지가 아니다
    캐시에 넣기 전에 호출해 잘못된 응답이 10분간 재사용되지 않게 한다."""
    head = text.lstrip("﻿ \t\r\n")
    snippet = re.sub(r"\s+", " ", _strip_tags(head))[:300]
    looks_html = head[:200].lower().startswith(("<!doctype", "<html"))
    if endpoint == "lawSearch.do" and "<totalCnt>" not in text:
        raise LawGoKrAuthError(
            f"law.go.kr가 검색 결과 대신 다른 응답을 돌려줌 — OC 값·IP/도메인 등록을 확인하세요. "
            f"응답 내용: {snippet or '(빈 응답)'} [진단] {diag}")
    if endpoint == "lawService.do" and (not head.startswith("<") or looks_html):
        raise LawGoKrAuthError(
            f"law.go.kr가 본문 대신 다른 응답을 돌려줌 — OC 값·IP/도메인 등록을 확인하세요. "
            f"응답 내용: {snippet or '(빈 응답)'} [진단] {diag}")


def _get(endpoint: str, **params) -> str:
    if not OC:
        raise LawGoKrAuthError("LAW_API_OC 환경변수가 설정되지 않았습니다 — law.go.kr에서 발급받은 기관코드(OC)를 설정하세요.")
    p = {"OC": OC, "type": "XML"}
    p.update({k: v for k, v in params.items() if v not in (None, "", 0)})
    key = (endpoint, tuple(sorted(p.items())))
    hit = _cache.get(key)
    now = time.time()
    if hit and hit[0] > now:
        return hit[1]
    # 파라미터 순서를 공식 샘플 URL과 같게 OC → target → type → 나머지로 고정한다.
    # dict 순서(OC, type, target …)로 보내면 Render에서 "필수입력요소 검증에 실패하였습니다.
    # 필수 입력값이 존재하지 않습니다"가 돌아왔다 (2026-11-20). 같은 서버에서 이 순서와
    # 브라우저 User-Agent를 쓰는 Law MCP(lawApi.ts)는 정상.
    head = [k for k in ("OC", "target", "type") if k in p]
    ordered = [(k, p[k]) for k in head] + [(k, v) for k, v in p.items() if k not in head]
    r = requests.get(f"{BASE}/{endpoint}", params=ordered, timeout=TIMEOUT,
                     headers=_HEADERS)
    r.raise_for_status()
    text = r.text
    if "인증" in text[:500] and "실패" in text[:500]:
        raise LawGoKrAuthError("law.go.kr 인증 실패 — OC 또는 IP 등록 확인 필요 (open.law.go.kr에서 현재 서버 IP 등록) "
                               f"[진단] {_diag(r)}")
    try:
        _check_response(endpoint, text, _diag(r))
    except LawGoKrAuthError as e:
        raise LawGoKrAuthError(f"{e} | 헤더=Referer·Origin 포함 | JSON재시도={_probe_json(endpoint, ordered)}") from None
    if len(_cache) >= _CACHE_MAX:
        del _cache[min(_cache, key=lambda k: _cache[k][0])]
    _cache[key] = (now + _CACHE_TTL, text)
    return text


def _parse_items(xml: str, item_tag: str):
    """<item_tag>…</item_tag> 블록마다 자식 태그를 dict로."""
    items = []
    for m in re.finditer(rf"<{item_tag}(?:\s[^>]*)?>(.*?)</{item_tag}>", xml, re.S):
        block = m.group(1)
        d = {}
        for tag, val in _TAG_RE.findall(block):
            d[tag] = _strip_cdata(val)
        if d:
            items.append(d)
    return items


class LawGoKrClient:
    # ---------- 판례 (법제처 제공 대법원·하급심) ----------
    def search_cases(self, keyword: str, court: str = "", date_from: str = "",
                     date_to: str = "", display: int = 10, page: int = 1):
        """target=prec 판례 검색. court: '대법원' 또는 '하위법원'(빈값=전체).
        date_from/date_to: YYYYMMDD (선고일자 범위)."""
        params = dict(target="prec", query=keyword, display=display, page=page, search=2)
        if court:
            params["curt"] = court
        if date_from or date_to:
            params["prncYd"] = f"{date_from or '19450815'}~{date_to or '20991231'}"
        xml = _get("lawSearch.do", **params)
        items = _parse_items(xml, "prec")
        total = re.search(r"<totalCnt>(\d+)</totalCnt>", xml)
        out = {
            "total": int(total.group(1)) if total else len(items),
            "cases": [{
                "판례일련번호": it.get("판례일련번호", ""),
                "사건명": it.get("사건명", ""),
                "사건번호": it.get("사건번호", ""),
                "법원명": it.get("법원명", ""),
                "선고일자": it.get("선고일자", ""),
                "판결유형": it.get("판결유형", ""),
                "사건종류명": it.get("사건종류명", ""),
            } for it in items],
        }
        if not out["cases"]:
            out["status"] = "NOT_FOUND"
        return out

    def get_case(self, case_serial: str, max_chars: int = 8000):
        """target=prec 판례 본문. case_serial = 판례일련번호."""
        xml = _get("lawService.do", target="prec", ID=case_serial)
        d = {}
        for tag in ("사건명", "사건번호", "법원명", "선고일자", "판시사항", "판결요지", "참조조문", "참조판례", "판례내용"):
            m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
            if m:
                d[tag] = _strip_tags(_strip_cdata(m.group(1)))[: max_chars if tag == "판례내용" else 4000]
        if not d:
            raise LawGoKrNotFound(f"판례 본문 없음 (일련번호 {case_serial}) — 응답 앞부분: {xml[:200]}")
        return d

    # ---------- 법령해석례 (법제처) ----------
    def search_interpretations(self, keyword: str, display: int = 10, page: int = 1):
        """target=expc 법령해석례 검색."""
        xml = _get("lawSearch.do", target="expc", query=keyword, display=display, page=page)
        items = _parse_items(xml, "expc")
        return [{
            "해석례일련번호": it.get("법령해석례일련번호", it.get("일련번호", "")),
            "안건명": it.get("안건명", ""),
            "안건번호": it.get("안건번호", ""),
            "회신기관": it.get("회신기관명", it.get("질의기관명", "")),
            "회신일자": it.get("회신일자", ""),
        } for it in items]

    def get_interpretation(self, serial: str, max_chars: int = 8000):
        xml = _get("lawService.do", target="expc", ID=serial)
        d = {}
        for tag in ("안건명", "안건번호", "회신일자", "질의요지", "회답", "이유"):
            m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
            if m:
                d[tag] = _strip_tags(_strip_cdata(m.group(1)))[:max_chars]
        if not d:
            raise LawGoKrNotFound(f"법령해석례 본문 없음 (일련번호 {serial}) — 응답 앞부분: {xml[:200]}")
        return d

    # ---------- 법령: 현행 검색 ----------
    def search_laws(self, law_name: str, display: int = 20):
        """target=law 현행 법령 검색 → 법령ID·MST(법령일련번호)·시행일자."""
        xml = _get("lawSearch.do", target="law", query=law_name, display=display)
        items = _parse_items(xml, "law")
        return [{
            "법령명": it.get("법령명한글", ""),
            "법령ID": it.get("법령ID", ""),
            "MST": it.get("법령일련번호", ""),
            "시행일자": it.get("시행일자", ""),
            "공포일자": it.get("공포일자", ""),
            "공포번호": it.get("공포번호", ""),
            "제개정구분": it.get("제개정구분명", ""),
        } for it in items]

    # ---------- 법령: 연혁(시행본) 목록 ----------
    def law_history(self, law_name: str, law_id: str = "", max_rows: int = 500):
        """target=eflaw 연혁 시행본 전체 목록. law_id를 주면 그 본법만 필터
        (예: 부가가치세법=001571 — 같은 이름 하위법령 혼입 방지).

        max_rows 기본 500: 소득세법처럼 시행본이 많은 법령은 100행에서 끊으면
        2011년 이전 연혁이 통째로 누락되어 law_article_as_of·law_article_diff가
        과거 시점을 조회하지 못한다 (2026-08-28 실측)."""
        rows = []
        page = 1
        while len(rows) < max_rows and page <= 10:
            # 페이지 크기 파라미터는 display가 맞음 — numOfRows는 무시되어 20건씩 5왕복하게 됨 (2026-08-18 실측)
            xml = _get("lawSearch.do", target="eflaw", query=law_name, display=100, page=page)
            items = _parse_items(xml, "law")
            if not items:
                break
            for it in items:
                if law_id and it.get("법령ID", "") != law_id:
                    continue
                rows.append({
                    "법령명": it.get("법령명한글", ""),
                    "법령ID": it.get("법령ID", ""),
                    "MST": it.get("법령일련번호", ""),
                    "시행일자": it.get("시행일자", ""),
                    "공포일자": it.get("공포일자", ""),
                    "공포번호": it.get("공포번호", ""),
                    "제개정구분": it.get("제개정구분명", ""),
                })
            page += 1
        # 같은 시행일자에 여러 공포본이 오는 일이 흔하다 — 조문별 단계 시행 때문에
        # 옛 공포본이 미래 시행일자 행으로 재등장하기 때문(2026-08-28 실측: 소득세법
        # 시행일자 20250101에 MST 6개). 시행일자만으로 정렬하면 '기준일 이하 마지막 행'을
        # 고르는 쪽(law_article_as_of·law_article_diff)이 그 그룹에서 가장 오래된 공포본을
        # 집어 옛 문안을 반환하므로, 공포일자·공포번호까지 정렬 키에 넣는다.
        rows.sort(key=lambda r: (r.get("시행일자", ""), r.get("공포일자", ""),
                                 (r.get("공포번호", "") or "").zfill(10)))
        return rows

    # ---------- 법령: 특정 시행본의 조문 원문 ----------
    @staticmethod
    def _find_article_block(xml: str, article_no: str):
        """법령 원문 XML에서 조문 블록을 슬라이스. 반환 (조문 라벨, 블록 or None).
        law_article(원문 반환)와 law_article_diff(구조적 비교)가 공유한다."""
        # 조문 뒤에 <부칙> 섹션(수십만 자)이 이어지므로 슬라이스 범위에서 잘라낸다 —
        # 안 자르면 마지막 조문 조회 시 부칙 전체가 본문에 딸려 나온다. 부칙은 law_addenda로.
        cut = xml.find("<부칙>")
        if cut != -1:
            xml = xml[:cut]
        base_no, branch_no = article_no.strip(), None
        m = re.match(r"^(\d+)의(\d+)$", article_no.strip())
        if m:
            base_no, branch_no = m.group(1), m.group(2)
        label = f"제{base_no}조" + (f"의{branch_no}" if branch_no else "")
        # <조문번호> 위치 순차 슬라이스 (단순 <조문>…</조문> 정규식은 실패 — 2026-07-21 확인)
        positions = [(mm.start(), mm.group(1)) for mm in re.finditer(r"<조문번호>(\d+)</조문번호>", xml)]
        for i, (pos, no) in enumerate(positions):
            if no != base_no:
                continue
            end = positions[i + 1][0] if i + 1 < len(positions) else len(xml)
            block = xml[pos:end]
            bm = re.search(r"<조문가지번호>(\d+)</조문가지번호>", block[:300])
            blk_branch = bm.group(1) if bm else None
            if branch_no != blk_branch:
                continue
            # 절/관/장이 이 조문에서 시작되면 그 표제가 동일 <조문번호>를 단 채
            # 실제 조문보다 먼저 나온다(예: 제104조 앞의 "제6절 …" 노드) — 표제 블록은
            # 조번호 문자열 자체를 포함하지 않으므로 걸러내고 다음 후보를 찾는다.
            if label not in _strip_tags(_strip_cdata(block)):
                continue
            return label, block
        return label, None

    def law_article(self, mst: str, article_no: str, max_chars: int = 6000):
        """lawService target=law&MST=… 원문에서 조번호 슬라이스.
        article_no: '17' 또는 '17의2' (패딩 없음)."""
        xml = _get("lawService.do", target="law", MST=mst)
        label, block = self._find_article_block(xml, article_no)
        if block is None:
            raise LawGoKrNotFound(f"MST {mst}에서 제{article_no}조를 찾지 못함")
        body = _strip_tags(_strip_cdata(block))
        title = re.search(r"<조문제목>(.*?)</조문제목>", block, re.S)
        out = {
            "조문": label,
            "조문제목": _strip_cdata(title.group(1)) if title else "",
        }
        # 조문단위마다 개별 시행일자가 달려 있다(같은 법이라도 조문별 시행일이 다를 수 있음)
        em = re.search(r"<조문시행일자>(\d+)</조문시행일자>", block)
        if em:
            out["조문시행일자"] = em.group(1)
        out["원문"] = body[:max_chars]
        if len(body) > max_chars:
            out["잘림"] = (f"전체 {len(body)}자 중 앞 {max_chars}자만 표시됨 — "
                         f"max_chars를 {len(body)} 이상으로 지정해 다시 조회하면 전문을 볼 수 있음")
        return out

    # ---------- 부칙 (시행일·적용례·경과조치) ----------
    @staticmethod
    def _parse_addenda_units(xml: str):
        """lawService target=law 응답 XML에서 <부칙단위> 목록을 파싱.
        law_addenda와 law_article_diff(개정 부칙 연결)가 공유한다."""
        units = []
        for m in re.finditer(r"<부칙단위[^>]*>(.*?)</부칙단위>", xml, re.S):
            blk = m.group(1)
            dt = re.search(r"<부칙공포일자>(\d+)</부칙공포일자>", blk)
            no = re.search(r"<부칙공포번호>(\d+)</부칙공포번호>", blk)
            bd = re.search(r"<부칙내용>(.*?)</부칙내용>", blk, re.S)
            body = _strip_cdata(bd.group(1)) if bd else ""
            # 머리말 "부칙 <제21221호,2025.12.23>"의 꺾쇠가 태그 제거에 지워지지 않게 보존
            body = re.sub(r"<(제\d+호[^>]*?)>", r"〈\1〉", body)
            body = _strip_tags(body)
            body = re.sub(r"[ \t]+\n", "\n", body)
            body = re.sub(r"\n{2,}", "\n", body)  # CDATA 조각 사이 빈 줄 압축
            units.append({
                "공포일자": dt.group(1) if dt else "",
                "공포번호": no.group(1).lstrip("0") if no else "",  # "04803" 식 패딩 제거
                "본문": body,
            })
        return units

    @staticmethod
    def _split_addendum(text: str):
        """부칙 본문을 '제N조(…)' 항 단위로 분할. 조 편제 없는 단순 부칙은 통짜 1개.
        구식 '제1조 (시행일)'처럼 조와 괄호 사이 공백이 있는 표기도 같은 앵커로 잡힌다."""
        starts = [m.start() for m in re.finditer(r"(?m)^제\d+조(?:의\d+)?\s*\(", text)]
        if not starts:
            t = text.strip()
            return [t] if t else []
        parts = []
        head = text[:starts[0]].strip()
        if head:
            parts.append(head)  # "부칙 <제21221호,2025.12.23>" 머리말
        for i, s in enumerate(starts):
            e = starts[i + 1] if i + 1 < len(starts) else len(text)
            parts.append(text[s:e].strip())
        return parts

    @staticmethod
    def _effective_summary(body: str, limit: int = 350):
        """부칙에서 시행일 부분만 요약 — 첫 '시행한다' 줄 + '각 호' 단서면 이어지는 호 목록."""
        m = re.search(r"(?m)^\s*(.*?시행한다.*)$", body)
        if not m:
            first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
            return first[:limit]
        lines = [m.group(1).strip()]
        if "각 호" in lines[0]:
            for ln in body[m.end():].splitlines():
                t = ln.strip()
                if not t:
                    continue
                if re.match(r"^\d+\.", t):
                    lines.append(t)
                else:
                    break
        out = " ".join(lines)
        return out[:limit] + ("…" if len(out) > limit else "")

    def law_addenda(self, mst: str = "", law_name: str = "", law_id: str = "",
                    as_of_date: str = "", promul_no: str = "", article_no: str = "",
                    recent: int = 10, max_chars: int = 8000):
        """법령 부칙(附則) 조회 — 개정규정의 시행일·적용례·경과조치.
        lawService target=law 응답의 <부칙단위> 목록을 파싱한다 (2026-08-26 실측:
        소득세법 현행본에 부칙 114건, 각각 공포일자·공포번호·본문 보유).

        - mst 미지정 시 law_name으로 시행본 자동 선택 (as_of_date 주면 그 시점본, 없으면 현행)
        - promul_no: 그 공포번호 개정의 부칙 전문 (공포번호는 law_history_search 목록에 있음)
        - article_no: 전체 부칙에서 그 조문이 언급된 항(적용례·경과조치)만 최신순 발췌
        - 둘 다 없으면: 최신순 recent건 목록 (공포일자·공포번호·시행일 요약)
        """
        if as_of_date and not re.match(r"^\d{8}$", str(as_of_date)):
            raise LawGoKrError(f"as_of_date 형식 오류: '{as_of_date}' (YYYYMMDD 8자리)")
        if not mst:
            if not law_name:
                raise LawGoKrError("mst 또는 law_name 중 하나는 필요합니다")
            if as_of_date:
                history = self.law_history(law_name, law_id=law_id)
                chosen = None
                for row in history:  # 시행일자 오름차순
                    if row["시행일자"] and row["시행일자"] <= as_of_date:
                        chosen = row
                if not chosen:
                    raise LawGoKrNotFound(f"{as_of_date} 이전 시행본 없음: {law_name}")
            else:
                rows = self.search_laws(law_name)
                if law_id:
                    rows = [r for r in rows if r.get("법령ID") == law_id]
                exact = [r for r in rows if r.get("법령명") == law_name.strip()]
                pick = exact or rows  # "소득세법" 검색에 시행령·시행규칙이 섞여 나와 정확 일치 우선
                if not pick:
                    raise LawGoKrNotFound(f"법령 검색 결과 없음: {law_name}")
                chosen = pick[0]
            mst = chosen["MST"]
        xml = _get("lawService.do", target="law", MST=mst)
        name = re.search(r"<법령명_한글>(.*?)</법령명_한글>", xml, re.S)
        # 원문 <기본정보>의 시행일자는 이 공포본 부칙의 본칙 시행일이다. 연혁 목록·검색의
        # '시행일자'(이 MST가 마지막으로 바뀐 날)와 다를 수 있다 — 예: 소득세법 MST 280405는
        # 목록상 20260701, 기본정보상 20260101 (2026-11-20).
        enf = re.search(r"<시행일자>(\d+)</시행일자>", xml)
        pno = re.search(r"<공포번호>(\d+)</공포번호>", xml)
        units = self._parse_addenda_units(xml)
        if not units:
            raise LawGoKrNotFound(f"MST {mst} 응답에 부칙 없음 — 응답 앞부분: {xml[:200]}")
        out = {
            "법령명": _strip_cdata(name.group(1)) if name else "",
            "MST": mst,
            "공포번호": pno.group(1) if pno else "",
            "본칙_시행일자": enf.group(1) if enf else "",
            "부칙총수": len(units),
        }
        # 조문별 시행일이 다른 개정이면 그 내역이 헤더에 요약돼 있다
        # (예: "20260701:제57조의2, … 20270101:제17조제3항, …")
        multi = re.search(r"<조문시행일자문자열>(.*?)</조문시행일자문자열>", xml, re.S)
        multi_txt = _strip_cdata(multi.group(1)) if multi else ""
        if multi_txt:
            out["조문별_상이한_시행일"] = multi_txt

        if promul_no:
            want = promul_no.strip().lstrip("0") or promul_no.strip()
            hits = [u for u in units if u["공포번호"] == want]
            if not hits:
                raise LawGoKrNotFound(
                    f"공포번호 {promul_no}의 부칙이 이 시행본에 없음 (부칙 {len(units)}건 보유) — "
                    f"law_history_search로 공포번호를 확인하세요")
            u = hits[-1]
            body = u["본문"]
            out["부칙"] = {"공포일자": u["공포일자"], "공포번호": u["공포번호"],
                          "본문": body[:max_chars]}
            if len(body) > max_chars:
                out["잘림"] = (f"이 부칙 전체 {len(body)}자 중 앞 {max_chars}자만 표시됨 — "
                             f"max_chars를 {len(body)} 이상으로 지정해 다시 조회")
            return out

        if article_no:
            a = article_no.strip()
            am = re.match(r"^(\d+)(?:의(\d+))?$", a)
            if not am:
                raise LawGoKrError(f"article_no 형식 오류: '{a}' (예: '96', '104의3')")
            label = f"제{am.group(1)}조" + (f"의{am.group(2)}" if am.group(2) else "")
            # '제96조' 검색이 '제96조의2' 언급에 오매치되지 않게 가지조문 아니면 (?!의) 가드
            pat = re.compile(re.escape(label) + ("" if am.group(2) else r"(?!의)"))
            found, used = [], 0
            for u in reversed(units):  # 최신 개정부터
                paras = [p[:1500] for p in self._split_addendum(u["본문"]) if pat.search(p)]
                if not paras:
                    continue
                size = sum(len(p) for p in paras)
                if found and used + size > max_chars:
                    out["잘림"] = (f"{label} 언급 부칙이 더 있으나 max_chars({max_chars}) 초과로 "
                                 f"최신 {len(found)}건까지만 표시 — max_chars를 늘려 다시 조회")
                    break
                used += size
                found.append({"공포일자": u["공포일자"], "공포번호": u["공포번호"],
                              "해당항": paras})
            out["조문"] = label
            out["언급된_부칙_최신순"] = found
            if not found:
                out["안내"] = (f"{label}이(가) 언급된 부칙 조항 없음 — 그 조문 개정에 별도 "
                             f"적용례·경과조치가 없었다는 뜻일 수 있음 (이때 시행일은 각 시행본의 "
                             f"시행일자·부칙 제1조를 따름)")
            return out

        rows = []
        for u in reversed(units):  # 최신순
            rows.append({"공포일자": u["공포일자"], "공포번호": u["공포번호"],
                         "시행일": self._effective_summary(u["본문"])})
            if len(rows) >= max(1, recent):
                break
        out["부칙목록_최신순"] = rows
        if len(units) > len(rows):
            out["안내"] = (f"전체 {len(units)}건 중 최신 {len(rows)}건만 표시 — recent를 늘리거나, "
                         f"특정 개정의 부칙 전문은 promul_no로, 특정 조문의 적용시기는 "
                         f"article_no로 조회")
        return out

    # ---------- 행정규칙 (훈령·예규·고시·기본통칙) ----------
    def search_admin_rules(self, keyword: str, display: int = 10, page: int = 1):
        """target=admrul 행정규칙 검색 — 기본통칙·조사사무처리규정·고시 등."""
        xml = _get("lawSearch.do", target="admrul", query=keyword, display=display, page=page)
        items = _parse_items(xml, "admrul")
        return [{
            "일련번호": it.get("행정규칙일련번호", ""),
            "행정규칙명": it.get("행정규칙명", ""),
            "종류": it.get("행정규칙종류", ""),
            "소관부처": it.get("소관부처명", ""),
            "발령일자": it.get("발령일자", ""),
            "발령번호": it.get("발령번호", ""),
            "시행일자": it.get("시행일자", ""),
        } for it in items]

    def get_admin_rule(self, serial: str, max_chars: int = 10000,
                       article: str = "", start_char: int = 0):
        """target=admrul 행정규칙 본문.
        article: 조번호를 주면 해당 조문만 슬라이스 — "9-5"(외국환거래규정식 제9-5조),
        "23", "23의2" 형식. 외국환거래규정(30만 자) 같은 대형 고시는 전문 반환이
        불가능하므로 조번호 지정이 사실상 필수. start_char: 조번호를 모를 때
        해당 위치부터 max_chars만큼 이어 읽는 오프셋."""
        xml = _get("lawService.do", target="admrul", ID=serial)
        name = re.search(r"<행정규칙명>(.*?)</행정규칙명>", xml, re.S)
        body = _strip_tags(_strip_cdata(xml))
        if len(body) < 50:
            return {"오류": "본문 없음", "응답": xml[:200]}
        out = {
            "행정규칙명": _strip_cdata(name.group(1)) if name else "",
            "본문길이": len(body),
        }
        if article:
            a = article.strip()
            am = re.match(r"^([\d-]+)(의\d+)?$", a)
            base, ui = (am.group(1), am.group(2) or "") if am else (a, "")
            # 조문 표제는 행 첫머리 "제9-5조(제목)" 형태 — 본문 중 인용("제7-31조의 규정 …")과
            # 행 앵커로 구분한다. 표제가 행 중간에 오는 규칙이면 못 찾으므로 start_char로 폴백.
            m = re.search(rf"(?m)^제{re.escape(base)}조{ui}(?=\(|\s|$)", body)
            if not m:
                out["오류"] = (f"제{a}조 표제를 찾지 못함 — 조번호 형식(예: '9-5', '23', '23의2')을 "
                             f"확인하거나 start_char 오프셋으로 조회")
                return out
            nm = re.search(r"(?m)^제[\d-]+조(?:의\d+)?\s*(?=\()", body[m.end():])
            seg = body[m.start():m.end() + nm.start()] if nm else body[m.start():]
            out["조문"] = f"제{a}조"
            out["본문"] = seg[:max_chars]
            if len(seg) > max_chars:
                out["잘림"] = (f"이 조문 전체 {len(seg)}자 중 앞 {max_chars}자만 표시됨 — "
                             f"max_chars를 {len(seg)} 이상으로 지정해 다시 조회")
            return out
        window = body[start_char:start_char + max_chars]
        out["본문"] = window
        if start_char + len(window) < len(body):
            out["잘림"] = (f"전체 {len(body)}자 중 {start_char}~{start_char + len(window)}자 구간만 표시됨 — "
                         f"start_char={start_char + len(window)}로 이어서 조회하거나, "
                         f"article에 조번호(예: '9-5')를 지정하면 해당 조문만 반환")
        return out

    # ---------- 조세조약 등 조약 ----------
    def search_treaties(self, keyword: str, display: int = 10, page: int = 1):
        """target=trty 조약 검색 — 조세조약 원문·발효일 확인용."""
        xml = _get("lawSearch.do", target="trty", query=keyword, display=display, page=page)
        items = _parse_items(xml, "Trty") + _parse_items(xml, "trty")
        return [{
            "조약일련번호": it.get("조약일련번호", ""),
            "조약명": it.get("조약명한글", it.get("조약명_한글", it.get("조약명", ""))),
            "조약구분": it.get("조약구분명", ""),
            "서명일자": it.get("서명일자", ""),
            "발효일자": it.get("발효일자", ""),
        } for it in items]

    # 조약 본문 조회 API(trtyInfoGuide) 출력 필드: 조약명_한글·조약명_영문·발효일자·서명일자·
    # 체결대상국가한글·조약내용 등. 국문/영문 본문은 별도 필드가 아니라 요청변수 chrClsCd로
    # 고른다(010202 한글 기본, 010203 영문). 예전 코드는 '조약명한글'(밑줄 없음)을 찾아
    # 조약명이 늘 비었고, 메타데이터까지 섞인 XML 전체를 본문으로 돌려줬다.
    _TREATY_LANG = {"ko": "010202", "en": "010203"}
    _TREATY_META = ("조약명_한글", "조약명_영문", "조약번호", "서명일자", "발효일자",
                    "체결대상국가한글", "양자조약분야명")

    @staticmethod
    def _treaty_text(raw: str) -> str:
        """<조약내용>을 줄 구조를 살려 텍스트로 — 조 표제를 행 첫머리에서 찾기 위해
        <p>·<br>·</div>를 줄바꿈으로 바꾼 뒤 태그를 지운다."""
        t = _strip_cdata(raw)
        # law.go.kr 조약 본문은 줄바꿈이 \r 단독인 경우가 있다(한·미 조세조약 실측) — 정규식 ^ 가
        # \n만 줄 경계로 보므로 먼저 \n으로 통일하지 않으면 조 표제를 하나도 찾지 못한다.
        t = t.replace("\r\n", "\n").replace("\r", "\n")
        t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", t)
        t = _strip_tags(t)
        t = html.unescape(t)
        t = re.sub(r"[ \t ]+\n", "\n", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t.strip()

    @staticmethod
    def _treaty_article_spans(body: str, article: str, lang: str):
        """조 표제 위치 목록 [(시작, 끝)]. 같은 조번호가 의정서 등에서 다시 나오면 여러 개."""
        a = article.strip()
        am = re.match(r"^(\d+)(?:의(\d+))?$", a)
        if not am:
            raise LawGoKrError(f"article 형식 오류: '{a}' (예: '10', '10의2')")
        num, branch = am.group(1), am.group(2)
        if lang == "en":
            head = r"(?im)^[ \t]*Article[ \t]+(\d+)(?:[ \t]*(?:bis|ter))?\b"
            want = num
        else:
            head = r"(?m)^[ \t]*제[ \t]*(\d+)[ \t]*조(?:[ \t]*의[ \t]*(\d+))?(?=[ \t(\n]|$)"
            want = (num, branch)
        # 표제는 짧은 한 줄이다. "제8조(사업소득)의 규정에 불구하고, …"처럼 다른 조를 인용하며
        # 시작하는 본문 문단을 표제로 잡으면 앞 조가 표제 한 줄만 남고 잘린다 (한·미 조약 제10조 실측).
        def _line_len(m):
            e = body.find("\n", m.start())
            return len(body[m.start(): e if e != -1 else len(body)].strip())
        heads = [m for m in re.finditer(head, body) if _line_len(m) <= 40]
        spans = []
        for i, m in enumerate(heads):
            key = m.group(1) if lang == "en" else (m.group(1), m.group(2))
            if key != want:
                continue
            end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
            spans.append((m.start(), end))
        return spans

    def get_treaty(self, serial: str, max_chars: int = 12000, article: str = "",
                   lang: str = "ko", start_char: int = 0, occurrence: int = 1):
        """target=trty 조약 본문.

        article: 조번호('10', '10의2')를 주면 그 조만 잘라 반환 — 조세조약은 수만 자라
            전문을 max_chars에 담을 수 없으므로 배당(10조)·이자(11조)·사용료(12조)·
            고정사업장(5조)처럼 조 단위로 읽는 것을 권장.
        lang: 'ko'(국문, 기본) | 'en'(영문 정본 — 해석이 갈릴 때 대조용)
        start_char: 조번호 없이 이어 읽을 때의 시작 오프셋
        occurrence: 같은 조번호가 의정서 등에서 다시 나올 때 몇 번째 것을 볼지 (1부터)
        """
        lang = (lang or "ko").lower()
        if lang not in self._TREATY_LANG:
            raise LawGoKrError(f"lang은 'ko' 또는 'en'이어야 합니다: '{lang}'")
        xml = _get("lawService.do", target="trty", ID=serial, chrClsCd=self._TREATY_LANG[lang])
        meta = {}
        for tag in self._TREATY_META:
            m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
            if m and _strip_cdata(m.group(1)):
                meta[tag] = _strip_tags(_strip_cdata(m.group(1)))
        cm = re.search(r"<조약내용>(.*?)</조약내용>", xml, re.S)
        if cm:
            body = self._treaty_text(cm.group(1))
        else:  # 필드명이 달라진 경우 대비 — 예전 동작(전체 텍스트)으로 폴백
            body = _strip_tags(_strip_cdata(xml))
            meta["주의"] = "<조약내용> 필드를 찾지 못해 응답 전체 텍스트를 본문으로 반환함"
        if len(body) < 50:
            raise LawGoKrNotFound(f"조약 본문 없음 (일련번호 {serial}, lang={lang}) — 응답 앞부분: {xml[:200]}")
        out = {"조약명": meta.pop("조약명_한글", ""), **meta, "언어": lang, "본문길이": len(body)}

        if article:
            spans = self._treaty_article_spans(body, article, lang)
            if not spans:
                out["오류"] = (f"제{article}조 표제를 찾지 못함 — 표제 표기가 다를 수 있음. "
                             f"start_char로 이어 읽거나 lang을 바꿔 시도")
                return out
            idx = max(1, int(occurrence or 1))
            if idx > len(spans):
                raise LawGoKrError(f"제{article}조는 {len(spans)}번만 나옵니다 (occurrence={idx})")
            s, e = spans[idx - 1]
            seg = body[s:e].strip()
            out["조"] = article
            out["본문"] = seg[:max_chars]
            if len(spans) > 1:
                out["안내"] = (f"같은 조번호가 본문에 {len(spans)}번 나옵니다(본 협약·의정서 등). "
                             f"지금은 {idx}번째 — occurrence로 다른 것을 볼 수 있음")
            if len(seg) > max_chars:
                out["잘림"] = f"이 조 전체 {len(seg)}자 중 앞 {max_chars}자만 표시 — max_chars를 늘려 재조회"
            return out

        window = body[start_char:start_char + max_chars]
        out["본문"] = window
        if start_char + len(window) < len(body):
            out["잘림"] = (f"전체 {len(body)}자 중 {start_char}~{start_char + len(window)}자 구간만 표시 — "
                         f"start_char={start_char + len(window)}로 이어 읽거나 article에 조번호를 지정")
        return out

    # ---------- 자치법규 (조례·규칙) ----------
    @staticmethod
    def _ordinance_row(it: dict) -> dict:
        return {
            "일련번호": it.get("자치법규일련번호", it.get("법령일련번호", "")),
            "자치법규명": it.get("자치법규명", it.get("법령명한글", "")),
            "지자체": it.get("지자체기관명", it.get("소관부처명", "")),
            "공포일자": it.get("공포일자", ""),
            "시행일자": it.get("시행일자", ""),
            "제개정구분": it.get("제개정구분명", ""),
        }

    _ORDIN_PAGE = 100     # 지역 필터 시 한 번에 받는 건수 (law.go.kr display 최대)
    _ORDIN_MAX_PAGES = 5  # 지역 필터 시 최대 500건까지 훑는다

    def search_ordinances(self, keyword: str, region: str = "", display: int = 20, page: int = 1):
        """target=ordin 자치법규 검색. region으로 지자체명 필터 (예: '서울', '용산구').

        지역 필터는 law.go.kr가 아니라 여기서 건다. 예전에는 display건만 받아 그 안에서
        걸러서, '시세 감면'+'부산'+display=3이면 부산광역시 조례가 4번째 이후라 0건으로
        나왔다 (2026-11-20). region이 있으면 페이지를 넘기며 충분히 받은 뒤 거른다."""
        if not region:
            xml = _get("lawSearch.do", target="ordin", query=keyword, display=display, page=page)
            items = _parse_items(xml, "law") + _parse_items(xml, "ordin")
            return [self._ordinance_row(it) for it in items]

        rows, scanned, total_n = [], 0, None
        for pg in range(1, self._ORDIN_MAX_PAGES + 1):
            xml = _get("lawSearch.do", target="ordin", query=keyword,
                       display=self._ORDIN_PAGE, page=pg)
            if total_n is None:
                t = re.search(r"<totalCnt>(\d+)</totalCnt>", xml)
                total_n = int(t.group(1)) if t else 0
            items = _parse_items(xml, "law") + _parse_items(xml, "ordin")
            scanned += len(items)
            rows += [r for r in map(self._ordinance_row, items) if region in r["지자체"]]
            if not items or scanned >= total_n:
                break
        start = (max(page, 1) - 1) * display
        picked = rows[start:start + display]
        if not picked:
            more = (f" (전체 {total_n}건 중 앞 {scanned}건만 확인 — 검색어에 지자체명을 넣어 좁히세요)"
                    if total_n and scanned < total_n else "")
            raise LawGoKrNotFound(
                f"'{keyword}' 검색 {total_n or 0}건 중 지자체명에 '{region}'이 든 자치법규 없음{more}")
        return picked

    @staticmethod
    def _ordinance_text(xml: str) -> str:
        """자치법규 본문 XML → 읽을 수 있는 텍스트 (조문 → 부칙 순).
        <조문><조><조문번호/><조문여부/><조제목/><조내용/></조>…</조문><부칙><부칙내용/></부칙>"""
        parts = []
        for m in re.finditer(r"<조내용>(.*?)</조내용>", xml, re.S):
            t = _strip_html(_strip_cdata(m.group(1))).strip()
            if t:
                parts.append(t)
        adds = [_strip_html(_strip_cdata(m.group(1))).strip()
                for m in re.finditer(r"<부칙내용>(.*?)</부칙내용>", xml, re.S)]
        adds = [a for a in adds if a]
        if adds:
            parts.append("[부칙]\n" + "\n".join(adds))
        text = "\n".join(parts).replace("\r\n", "\n").replace("\r", "\n")
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    def get_ordinance(self, serial: str, max_chars: int = 10000, start_char: int = 0):
        """target=ordin 자치법규 본문.

        serial은 검색 결과의 '일련번호'(자치법규일련번호)이므로 MST로 보낸다. 예전에는 ID로
        보내 자치법규ID가 우연히 같은 다른 조례가 왔다 (부산광역시 시세 감면 조례 2130441 →
        태백시 기업 및 투자유치 지원에 관한 조례 시행규칙, 2026-11-20)."""
        serial = str(serial).strip()
        if not serial.isdigit():
            raise LawGoKrError(f"serial은 검색 결과의 일련번호(숫자): '{serial}'")
        xml = _get("lawService.do", target="ordin", MST=serial)

        def field(tag):
            m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
            return _strip_cdata(m.group(1)) if m else ""

        got = field("자치법규일련번호")
        if got and got != serial:
            raise LawGoKrParseError(f"요청한 일련번호 {serial}와 다른 자치법규({got})가 응답됨 — 조회 중단")
        body = self._ordinance_text(xml)
        if len(body) < 20:
            raise LawGoKrNotFound(f"일련번호 {serial}의 본문 없음 — 응답 앞부분: {xml[:200]}")
        start_char = max(int(start_char or 0), 0)
        window = body[start_char:start_char + max_chars]
        out = {
            "자치법규명": field("자치법규명"),
            "지자체": field("지자체기관명"),
            "일련번호": got or serial,
            "자치법규ID": field("자치법규ID"),
            "공포일자": field("공포일자"),
            "공포번호": field("공포번호"),
            "시행일자": field("시행일자"),
            "제개정구분": field("제개정정보"),
            "본문": window,
            "본문길이": len(body),
        }
        if start_char + len(window) < len(body):
            out["잘림"] = (f"전체 {len(body)}자 중 {start_char}~{start_char + len(window)}자만 표시 — "
                         f"start_char={start_char + len(window)}로 이어 읽기")
        return out

    # ---------- 공통: 시행본 선택 · 조문 목록 ----------
    def _resolve_version(self, law_name: str = "", law_id: str = "", as_of_date: str = "",
                         mst: str = ""):
        """조회할 시행본 MST를 고른다. 반환 (mst, 메타 dict).
        mst 직접 지정 > as_of_date 당시 시행본 > 현행본(법령명 정확 일치 우선)."""
        if as_of_date and not re.match(r"^\d{8}$", str(as_of_date)):
            raise LawGoKrError(f"as_of_date 형식 오류: '{as_of_date}' (YYYYMMDD 8자리)")
        if mst:
            return str(mst), {"MST": str(mst)}
        if not law_name:
            raise LawGoKrError("law_name 또는 mst 중 하나는 필요합니다")
        if as_of_date:
            chosen = None
            for row in self.law_history(law_name, law_id=law_id):  # 시행일자 오름차순
                if row["시행일자"] and row["시행일자"] <= as_of_date:
                    chosen = row
            if not chosen:
                raise LawGoKrNotFound(f"{as_of_date} 이전 시행본 없음: {law_name}")
        else:
            rows = self.search_laws(law_name)
            if law_id:
                rows = [r for r in rows if r.get("법령ID") == law_id]
            exact = [r for r in rows if r.get("법령명") == law_name.strip()]
            pick = exact or rows  # "소득세법" 검색에 시행령·시행규칙이 섞여 나와 정확 일치 우선
            if not pick:
                raise LawGoKrNotFound(f"법령 검색 결과 없음: {law_name}")
            chosen = pick[0]
        meta = {k: chosen.get(k, "") for k in ("법령명", "시행일자", "공포일자", "공포번호", "MST")}
        return chosen["MST"], meta

    @staticmethod
    def _iter_articles(xml: str):
        """법령 원문 XML의 실제 조문(<조문여부>조문)만 순서대로.
        장·절 표제(<조문여부>전문)와 부칙은 제외한다."""
        cut = xml.find("<부칙>")
        if cut != -1:
            xml = xml[:cut]
        out = []
        for m in re.finditer(r"<조문단위[^>]*>(.*?)</조문단위>", xml, re.S):
            blk = m.group(1)
            kind = re.search(r"<조문여부>(.*?)</조문여부>", blk)
            if kind and _strip_cdata(kind.group(1)) != "조문":
                continue
            no = re.search(r"<조문번호>(\d+)</조문번호>", blk)
            if not no:
                continue
            br = re.search(r"<조문가지번호>(\d+)</조문가지번호>", blk)
            title = re.search(r"<조문제목>(.*?)</조문제목>", blk, re.S)
            ed = re.search(r"<조문시행일자>(\d+)</조문시행일자>", blk)
            ch = re.search(r"<조문변경여부>(\w)</조문변경여부>", blk)
            out.append({
                "조문": _article_label(no.group(1), br.group(1) if br else ""),
                "조문제목": _strip_cdata(title.group(1)) if title else "",
                "조문시행일자": ed.group(1) if ed else "",
                "변경": (ch.group(1) == "Y") if ch else None,
                "block": blk,
            })
        return out

    # ---------- 조문 키워드 검색 (한 법령 안에서) ----------
    def law_article_search(self, law_name: str, keyword: str, law_id: str = "",
                           as_of_date: str = "", mst: str = "", max_results: int = 20,
                           snippet_chars: int = 160):
        """법령 원문에서 키워드가 들어간 조문을 찾는다. 공백으로 나눈 단어는 모두
        포함해야 일치(AND). 조문 제목 일치를 본문 일치보다 앞에 둔다."""
        terms = [t for t in (keyword or "").split() if t]
        if not terms:
            raise LawGoKrError("keyword가 비어 있습니다")
        mst, meta = self._resolve_version(law_name, law_id, as_of_date, mst)
        xml = _get("lawService.do", target="law", MST=mst)
        name = re.search(r"<법령명_한글>(.*?)</법령명_한글>", xml, re.S)
        arts = self._iter_articles(xml)
        if not arts:
            raise LawGoKrParseError(f"MST {mst} 응답에서 조문을 찾지 못함 — 응답 앞부분: {xml[:200]}")
        hits = []
        for a in arts:
            text = "\n".join(self._article_content_lines(a["block"]))
            hay = a["조문제목"] + "\n" + text
            if not all(t in hay for t in terms):
                continue
            pos = text.find(terms[0])
            half = max(40, snippet_chars // 2)
            if pos == -1:
                snip = text[:snippet_chars]
            else:
                s = max(0, pos - half)
                snip = ("…" if s else "") + text[s:pos + half] + ("…" if pos + half < len(text) else "")
            hits.append({
                "조문": a["조문"], "조문제목": a["조문제목"],
                "일치횟수": sum(hay.count(t) for t in terms),
                "발췌": snip.replace("\n", " "),
                "_title_hit": any(t in a["조문제목"] for t in terms),
            })
        hits.sort(key=lambda h: not h.pop("_title_hit"))  # 안정 정렬 — 같은 그룹 안은 조문 순서 유지
        out = {
            "법령명": _strip_cdata(name.group(1)) if name else meta.get("법령명", ""),
            "시행본": meta,
            "검색어": terms,
            "일치조문수": len(hits),
            "결과": hits[:max(1, max_results)],
        }
        if len(hits) > max_results:
            out["안내"] = f"일치 {len(hits)}건 중 {max_results}건만 표시 — 검색어를 더 붙여 좁히거나 max_results를 늘리세요"
        if not hits:
            out["status"] = "NOT_FOUND"
            out["_guidance"] = ("이 시행본의 조문에 검색어가 없음 (조회 자체는 정상). 시행령·시행규칙에 "
                                "있을 수 있으니 law_name을 바꿔 보거나 단어를 줄여 재검색")
        out["조회방법"] = "조문 전문은 law_article_as_of(law_name, 날짜, 조번호)로 조회"
        return out

    # ---------- 시행 예정 개정 (공포됐으나 아직 시행 전) ----------
    @staticmethod
    def _article1_section(addendum_body: str) -> str:
        """부칙 제1조(시행일) 본문 — 조 편제 없는 단순 부칙이면 머리말 다음 전체."""
        m = re.search(r"(?m)^\s*제1조\s*\([^)]*\)", addendum_body)
        if not m:
            lines = addendum_body.splitlines()
            return "\n".join(lines[1:]) if len(lines) > 1 else addendum_body
        nxt = re.search(r"(?m)^\s*제2조\s*\(", addendum_body[m.end():])
        return addendum_body[m.end(): m.end() + nxt.start()] if nxt else addendum_body[m.end():]

    @classmethod
    def _main_effective_date(cls, addendum_body: str, promul_date: str, target: str) -> bool:
        """부칙 제1조 본문('이 법은 …부터 시행한다. 다만, …')의 본칙 시행일이 target(YYYYMMDD)인가.
        시행본 XML의 <시행일자>는 그 시행본의 날짜라 시행예정본에서는 늘 target과 같으므로
        본칙 판정에 쓸 수 없다 (2026-11-20 실측: 2024년 시행된 제19563호를 2027년 본칙으로 오표시).
        '공포한 날'·'공포 후 N일/개월/년이 경과한 날'도 처리 (경과일 계산은 ±1일 허용)."""
        import datetime as _dt
        sec = cls._article1_section(addendum_body)
        main = re.split(r"다만", sec, maxsplit=1)[0]
        ymd = _korean_date_to_ymd(main)
        if ymd:
            return ymd == target
        if re.search(r"공포한\s*날", main):
            return promul_date == target
        m = re.search(r"공포\s*후\s*(\d+)\s*(일|개월|년)", main)
        if m and len(promul_date) == 8 and len(target) == 8:
            try:
                d = _dt.date(int(promul_date[:4]), int(promul_date[4:6]), int(promul_date[6:]))
                t = _dt.date(int(target[:4]), int(target[4:6]), int(target[6:]))
            except ValueError:
                return False
            n, unit = int(m.group(1)), m.group(2)
            if unit == "일":
                cand = d + _dt.timedelta(days=n)
            else:
                months = n if unit == "개월" else n * 12
                import calendar as _cal
                y, mo = divmod(d.month - 1 + months, 12)
                yy, mm = d.year + y, mo + 1
                cand = _dt.date(yy, mm, min(d.day, _cal.monthrange(yy, mm)[1]))
            return abs((t - cand).days) <= 3
        return False

    @classmethod
    def _staged_items(cls, addendum_body: str):
        """부칙 제1조 단서의 '각 호' 목록 → [(시행일 YYYYMMDD, 호 문장)].
        예: '2. 제17조제3항 및 제118조의9부터 …: 2027년 1월 1일' → ('20270101', '2. …')"""
        out = []
        sec = cls._article1_section(addendum_body)
        for ln in sec.splitlines():
            t = ln.strip()
            if re.match(r"^\d+\.", t):
                d = _korean_date_to_ymd(t.rsplit(":", 1)[-1]) or _korean_date_to_ymd(t)
                if d:
                    out.append((d, t))
        return out

    @classmethod
    def _later_addenda_refs(cls, units, promul_no: str, target: str, promul_date: str):
        """다른(이후) 공포본 부칙 제1조 단서 중 '법률 제{promul_no}호 …의 개정규정: {target}'처럼
        이 공포본 조문의 시행일을 정한 호 → [{공포번호, 공포일자, 문장, 대상}]. '대상'은
        '법률 제N호' 뒤쪽(그 공포본 조문 부분)만 잘라 둔 것이다."""
        key = re.compile(cls._ACT_KIND + rf"\s*제{promul_no}호")
        refs = []
        for u in units:
            if u["공포번호"] == promul_no or (u.get("공포일자") or "") <= promul_date:
                continue
            for d, t in cls._staged_items(u["본문"]):
                km = key.search(t) if d == target else None
                if km:
                    refs.append({"공포번호": u["공포번호"], "공포일자": u.get("공포일자", ""),
                                 "문장": t, "대상": t[km.end():].split(":")[0]})
        return refs

    _ART_RE = r"제(\d+)조(?:의(\d+))?(?:제\d+항)?(?:제\d+호)?"
    # 법령 종류 표기 — 법률은 '법률 제N호 ○○법 일부개정법률', 시행령은 '대통령령 제N호 ○○법 시행령
    # 일부개정령', 시행규칙은 '총리령/○○부령 제N호 … 일부개정령'으로 쓴다 (2026-11-20 실측:
    # 소득세법 시행령 제35349호 부칙 '대통령령 제32516호 소득세법 시행령 일부개정령 제88조…').
    _ACT_KIND = r"(?:법률|대통령령|총리령|[가-힣]+부령)"
    _ACT_SUFFIX = r"일부개정(?:법률|령|규칙)"
    _AMEND_LAW_RE = re.compile(_ACT_KIND + r"\s*제(\d+)호\s*(.+?)\s*" + _ACT_SUFFIX
                               + r"\s*일부를\s*다음과\s*같이\s*개정한다")
    # 블록 안의 한 줄짜리 개정: '대통령령 제32516호 소득세법 시행령 일부개정령(…) 제216조의4 중 …'
    _AMEND_INLINE_RE = re.compile(r"^" + _ACT_KIND + r"\s*제(\d+)호\s*(.+?)\s*" + _ACT_SUFFIX
                                  + r"\s*(제\d+조(?:의\d+)?)")
    _AMEND_LAW_MARK = (r"(" + _ACT_KIND + r"\s*제\d+호\s*\S+(?:\s\S+)?\s*" + _ACT_SUFFIX + r")")

    @staticmethod
    def _drop_parens(text: str) -> str:
        """괄호 안('(…에 따라 개정된 내용을 포함한다)', '(…에 한정한다)')을 지운다 — 바깥쪽 문장이
        어느 공포본의 어느 조문인지만 가리기 위해."""
        prev = None
        while prev != text:
            prev, text = text, re.sub(r"\([^()]*\)", "", text)
        return text

    @classmethod
    def _labels(cls, head: str, order=None):
        """'제17조제3항 및 제118조의9부터 제118조의18까지' → ['제17조', '제118조의9', …].
        order(원문 조문 순서)가 있으면 '…부터 …까지' 범위를 펼친다."""
        labels = []
        if order:
            for rm in re.finditer(cls._ART_RE + r"\s*부터\s*" + cls._ART_RE + r"\s*까지", head):
                a0 = _article_label(rm.group(1), rm.group(2) or "")
                a1 = _article_label(rm.group(3), rm.group(4) or "")
                if a0 in order and a1 in order and order.index(a0) <= order.index(a1):
                    for lb in order[order.index(a0): order.index(a1) + 1]:
                        if lb not in labels:
                            labels.append(lb)
        for lm in re.finditer(r"제(\d+)조(?:의(\d+))?", head):
            lb = _article_label(lm.group(1), lm.group(2) or "")
            if lb not in labels:
                labels.append(lb)
        for bm in re.finditer(r"별표\s*(\d+)(?:의(\d+))?", head):  # '별표 5의 개정규정'
            lb = f"별표 {bm.group(1)}" + (f"의{bm.group(2)}" if bm.group(2) else "")
            if lb not in labels:
                labels.append(lb)
        return labels

    @classmethod
    def _amended_law_links(cls, text: str, law_name: str = ""):
        """부칙(대개 타법개정의 '다른 법률의 개정')에서 아직 시행 전인 개정법령을 고친 대목을 찾는다.
        ① 블록형: '⑥ 법률 제17757호 소득세법 일부개정법률 일부를 다음과 같이 개정한다.' 다음 줄들의
           '제21조제1항제27호 중 …'
        ② 한 줄형: '대통령령 제32516호 소득세법 시행령 일부개정령(…) 제216조의4 중 …로 한다.'
        → [{'개정법': '17757', '조문': ['제21조', …], '문장': [...]}] (같은 개정법은 하나로 합침)"""
        lines = [ln.strip() for ln in (text or "").splitlines()]
        want_name = re.sub(r"\s+", "", law_name or "")

        def name_ok(name):
            return not want_name or re.sub(r"\s+", "", name) == want_name

        found = {}

        def add(no, labels, body):
            km = re.search(r"(" + cls._ACT_KIND + r")\s*제" + no + "호", body[0])
            d = found.setdefault(no, {"개정법": no, "종류": km.group(1) if km else "",
                                      "조문": [], "문장": []})
            d["조문"] += [lb for lb in labels if lb not in d["조문"]]
            d["문장"] += [t for t in body if t not in d["문장"]]

        i = 0
        while i < len(lines):
            t0 = lines[i]
            flat = cls._drop_parens(t0)
            im = cls._AMEND_INLINE_RE.search(flat)
            if im and name_ok(im.group(2)):
                lm = re.match(r"제(\d+)조(?:의(\d+))?", im.group(3))
                add(im.group(1), [_article_label(lm.group(1), lm.group(2) or "")], [t0])
                i += 1
                continue
            m = cls._AMEND_LAW_RE.search(t0)
            i += 1
            if not m or not name_ok(m.group(2)):
                continue
            labels, body = [], [t0]
            while i < len(lines):
                t = lines[i]
                if t and (re.match(r"^(?:[①-⑳]|<\d+>|제\d+조(?:의\d+)?\s*\()", t)
                          or cls._AMEND_LAW_RE.search(t) or cls._AMEND_INLINE_RE.search(cls._drop_parens(t))):
                    break
                lm = re.match(r"^제(\d+)조(?:의(\d+))?", t)
                if lm:
                    lb = _article_label(lm.group(1), lm.group(2) or "")
                    if lb not in labels:
                        labels.append(lb)
                    body.append(t)
                i += 1
            if labels:
                add(m.group(1), labels, body)
        return list(found.values())

    @classmethod
    def _target_day_items(cls, units, target: str):
        """모든 부칙 중 target에 시행되는 것을, 가리키는 조문이 어느 공포본 소속인지로 나눈다.
        - 제1조 단서 각 호: '법률/대통령령 제N호 … 일부개정법률/령 …'이면 N, 아니면 그 부칙의 공포번호
        - 본칙 시행일이 target인 부칙: 그 공포본 전체(조문 '*')"""
        out = []
        for u in units:
            if cls._main_effective_date(u["본문"], u.get("공포일자", ""), target):
                sec = cls._article1_section(u["본문"])
                out.append({"부칙": u["공포번호"], "부칙_공포일자": u.get("공포일자", ""),
                            "소속": u["공포번호"], "조문": ["*"],
                            "문장": re.split(r"(?<=다\.)\s", sec.strip(), maxsplit=1)[0]})
            for d, t in cls._staged_items(u["본문"]):
                if d != target:
                    continue
                parts = re.split(cls._AMEND_LAW_MARK, cls._drop_parens(t.split(":")[0]))
                segs = [(u["공포번호"], parts[0])]
                for k in range(1, len(parts) - 1, 2):
                    segs.append((re.search(r"제(\d+)호", parts[k]).group(1), parts[k + 1]))
                for owner, seg in segs:
                    labels = cls._labels(seg)
                    if labels:
                        out.append({"부칙": u["공포번호"], "부칙_공포일자": u.get("공포일자", ""),
                                    "소속": owner, "조문": labels, "문장": t})
        return out

    def law_upcoming(self, law_name: str, law_id: str = "", max_versions: int = 5,
                     today: str = ""):
        """공포됐으나 아직 시행되지 않은 개정(시행예정 시행본)과, 시행일마다 어떤
        조문이 바뀌는지를 정리한다.

        근거 우선순위: ① 그 공포본 부칙 제1조 단서의 '각 호' (조문별 시행일이 다른 경우 —
        2026-08 실측 소득세법 제21221호: '2. 제17조제3항 및 제118조의9부터 …: 2027년 1월 1일')
        ② 본칙 시행일이 그날이면 그 공포본에서 <조문변경여부>Y인 조문(단서 호에 든 조문 제외).
        """
        today = today or time.strftime("%Y%m%d")
        history = self.law_history(law_name, law_id=law_id)
        if not history:
            raise LawGoKrNotFound(f"연혁 없음: {law_name}")
        future = [r for r in history if (r.get("시행일자") or "") > today]
        out = {"법령명": history[-1].get("법령명", law_name), "기준일": today,
               "시행예정_건수": len(future)}
        if not future:
            out["status"] = "NOT_FOUND"
            out["_guidance"] = (f"{today} 이후 시행 예정인 시행본 없음 (연혁 조회는 정상) — "
                                f"공포 전 개정안(국회 계류·입법예고)은 이 자료에 없음")
            return out
        # 시행일 오름차순, 같은 날 같은 MST 중복 행 제거
        seen, rows = set(), []
        for r in sorted(future, key=lambda r: (r["시행일자"], r.get("공포일자", ""))):
            k = (r["시행일자"], r["MST"])
            if k not in seen:
                seen.add(k)
                rows.append(r)
        xml_cache, versions = {}, []
        # 시행예정본 XML의 부칙은 그 공포 당시 문안이다. 이후 법률이 부칙 시행일을 바꾸면
        # 가장 최근 공포본 XML의 부칙에만 바뀐 문안(과 바꾼 법률의 부칙)이 있다
        # (2026-11-20 실측: 제17757호 가상자산 과세 → 현재 부칙 2호 '2027년 1월 1일',
        #  바꾼 법률 제20615호 부칙 제1조 2호).
        latest = max(history, key=lambda r: (r.get("공포일자", ""),
                                             int(r.get("공포번호") or 0)))
        latest_units = None

        def _latest_units():
            nonlocal latest_units
            if latest_units is None:
                try:
                    if latest["MST"] not in xml_cache:
                        xml_cache[latest["MST"]] = _get("lawService.do", target="law", MST=latest["MST"])
                    latest_units = self._parse_addenda_units(xml_cache[latest["MST"]])
                except (LawGoKrError, requests.exceptions.RequestException):
                    latest_units = []
            return latest_units

        for r in rows[:max(1, max_versions)]:
            v = {k: r.get(k, "") for k in ("시행일자", "공포일자", "공포번호", "제개정구분", "MST")}
            try:
                if r["MST"] not in xml_cache:
                    xml_cache[r["MST"]] = _get("lawService.do", target="law", MST=r["MST"])
                xml = xml_cache[r["MST"]]
            except (LawGoKrAuthError, requests.exceptions.RequestException) as e:
                v["오류"] = f"시행본 원문 조회 실패 — {type(e).__name__}: {e}"
                versions.append(v)
                continue
            arts = self._iter_articles(xml)
            titles = {a["조문"]: a["조문제목"] for a in arts}
            base = re.search(r"<시행일자>(\d+)</시행일자>", xml)
            base_date = base.group(1) if base else ""
            want = (r.get("공포번호") or "").lstrip("0")
            unit = next((u for u in self._parse_addenda_units(xml) if u["공포번호"] == want), None)
            staged = self._staged_items(unit["본문"]) if unit else []
            if unit:
                v["부칙_시행일"] = self._effective_summary(unit["본문"], limit=600)
            todays = [t for d, t in staged if d == r["시행일자"]]
            basis = "부칙 제1조 단서 각 호 (조문별 시행일)"
            is_main = bool(unit) and self._main_effective_date(
                unit["본문"], (r.get("공포일자") or ""), r["시행일자"])
            if not todays and unit and not is_main:
                # 호 목록 없이 단서 문장으로 쓴 부칙: "다만, 제x조의 개정규정은 2027년 1월 1일부터 …"
                sec = self._article1_section(unit["본문"])
                for sent in re.split(r"(?<=다\.)\s*|\n", sec):
                    if _korean_date_to_ymd(sent) == r["시행일자"] and re.search(r"제\d+조", sent):
                        todays.append(sent.strip())
                basis = "부칙 제1조 단서 문장 (조문별 시행일)"
            later_refs, cur, main_note, later_labels = [], None, "", None
            if not todays and not is_main and want:
                lu = _latest_units()
                cur = next((u for u in lu if u["공포번호"] == want), None)
                if cur is not None and cur["본문"] != (unit or {}).get("본문"):
                    todays = [t for d, t in self._staged_items(cur["본문"]) if d == r["시행일자"]]
                    if todays:
                        basis = ("부칙 제1조 단서 각 호 — 이후 법률로 바뀐 현재 부칙 문안 "
                                 f"(최신 공포본 제{latest.get('공포번호', '')}호 원문 기준)")
                    elif self._main_effective_date(cur["본문"], r.get("공포일자") or "", r["시행일자"]):
                        # 본칙 시행일 자체가 바뀐 경우 (2026-11-20 실측: 소득세법 시행령 제32516호
                        # '이 영은 2027년 1월 1일부터 시행한다. <개정 2022.12.31, 2024.12.31>')
                        is_main, main_note = True, (
                            f" (이후 개정으로 바뀐 현재 부칙 문안 기준, 최신 공포본 제{latest.get('공포번호', '')}호 원문)")
                        staged = self._staged_items(cur["본문"])
                later_refs = self._later_addenda_refs(lu, want, r["시행일자"], r.get("공포일자") or "")
                if not todays and not is_main and later_refs:
                    todays = [ref["문장"] for ref in later_refs]
                    later_labels = []
                    for ref in later_refs:  # 조문은 '제N호 …' 뒤쪽(이 공포본 조문 부분)에서만 뽑는다
                        later_labels += [lb for lb in self._labels(ref["대상"]) if lb not in later_labels]
                    basis = "후속 법령 부칙이 이 공포본 조문의 시행일을 정함"
            if later_refs:
                v["시행일_변경_근거"] = [{k: ref[k] for k in ("공포번호", "공포일자", "문장")}
                                     for ref in later_refs]
            order = [a["조문"] for a in arts]
            forced_labels = later_labels
            if not todays and not is_main and want:
                # ③ 이 공포본이 '아직 시행 전인 개정법'을 고친 경우(타법개정 부칙의 '다른 법률의
                #    개정'에 흔함) — 고친 조문은 그 개정법 부칙의 날짜에 시행된다.
                #    2026-11-20 실측: 제19563호(가상자산 이용자 보호법) 부칙 제2조⑥이 법률 제17757호의
                #    제21조·제119조를 고침 → 제17757호 부칙 2호 '2027년 1월 1일'.
                items = self._target_day_items(lu, r["시행일자"])
                text = (cur or unit or {}).get("본문", "")
                for link in self._amended_law_links(text, out["법령명"]):
                    hits = [it for it in items if it["소속"] == link["개정법"]
                            and ("*" in it["조문"] or set(it["조문"]) & set(link["조문"]))]
                    if hits:
                        common = [lb for lb in link["조문"]
                                  if any("*" in it["조문"] or lb in it["조문"] for it in hits)]
                        todays = link["문장"]
                        forced_labels = common
                        ref_name = f"{link.get('종류', '')} 제{link['개정법']}호".strip()
                        basis = (f"이 공포본 부칙이 아직 시행 전이던 {ref_name}를 고침 — "
                                 f"고친 조문은 제{link['개정법']}호의 시행일(그날)을 따른다")
                        v["연결_근거"] = [{"부칙": it["부칙"], "부칙_공포일자": it["부칙_공포일자"],
                                         "문장": it["문장"]} for it in hits]
                        break
                if not todays:
                    # ④ (추정) 이 공포본이 개정 표시한 조문이, 이보다 먼저 공포된 개정법의 조문으로서
                    #    그날 시행되는 항목에 들어 있으면 그 연결을 보여준다 — 근거 문장이 없으므로 추정.
                    mine_date = r.get("공포일자") or ""
                    pdates = {u["공포번호"]: u.get("공포일자", "") for u in lu}
                    changed = {a["조문"] for a in arts if a["변경"]}
                    guess = []
                    for it in items:
                        if it["소속"] == want or not (pdates.get(it["소속"], "9") < mine_date):
                            continue
                        common = [lb for lb in it["조문"] if lb in changed]  # '*'는 추정에 쓰지 않음
                        if common:
                            guess.append(dict(it, 겹치는_조문=common))
                    if guess:
                        labels_g = []
                        for g in guess:
                            labels_g += [lb for lb in g["겹치는_조문"] if lb not in labels_g]
                        todays = [g["문장"] for g in guess]
                        forced_labels = labels_g
                        basis = ("추정 — 근거 문장 없음. 이 공포본이 개정한 조문 중 일부가, 먼저 공포된 "
                                 "개정법의 조문으로서 그날 시행되는 항목에 들어 있음")
                        v["연결_추정"] = [{"소속_공포번호": g["소속"], "부칙": g["부칙"],
                                         "겹치는_조문": g["겹치는_조문"], "문장": g["문장"]}
                                        for g in guess]
            if todays:
                labels = (list(forced_labels) if forced_labels is not None else [])
                if forced_labels is None:
                    for t in todays:
                        for lb in self._labels(t.split(":")[0], order):
                            if lb not in labels:
                                labels.append(lb)
                labels.sort(key=lambda lb: order.index(lb) if lb in order else len(order))
                v["근거"] = basis
                v["이날_시행되는_개정규정"] = todays
                v["언급_조문"] = [{"조문": lb, "조문제목": titles.get(lb, "")} for lb in labels]
            elif is_main:
                staged_labels = {(_article_label(m.group(1), m.group(2) or ""))
                                 for _d, t in staged
                                 for m in re.finditer(r"제(\d+)조(?:의(\d+))?", t.split(":")[0])}
                changed = [{"조문": a["조문"], "조문제목": a["조문제목"]}
                           for a in arts if a["변경"] and a["조문"] not in staged_labels]
                v["근거"] = "본칙 시행일 — 이 공포본에서 개정 표시(<조문변경여부>Y)된 조문" + main_note
                v["개정_조문"] = changed
                if staged:
                    v["참고"] = "부칙 단서 호에 든 조문은 각 호의 날짜에 따로 시행되므로 제외함"
                if not changed and later_refs:
                    # 시행예정본 원문에 개정 표시가 없으면(시행령 제32516호 실측) 후속 부칙이 그날로
                    # 정한 조문이라도 보여준다
                    labels = []
                    for ref in later_refs:
                        labels += [lb for lb in self._labels(ref["대상"]) if lb not in labels]
                    labels.sort(key=lambda lb: order.index(lb) if lb in order else len(order))
                    v["언급_조문"] = [{"조문": lb, "조문제목": titles.get(lb, "")} for lb in labels]
                    v["참고"] = ("시행예정본 원문에 개정 표시(<조문변경여부>Y)가 없어, 시행일_변경_근거의 "
                                "후속 부칙이 그날 시행으로 정한 조문을 언급_조문으로 보여줌")
            else:
                v["근거"] = "확인 불가"
                v["안내"] = ("이 시행일은 이 공포본 부칙(공포 당시·현재 문안 모두)의 본칙 시행일도, 단서 각 호의 "
                            "날짜도 아니고, 이후 법률 부칙에도 이 공포본을 그날로 정한 호가 없으며, 이 공포본이 "
                            "고친 미시행 개정법과의 연결도 찾지 못함. "
                            "법제처 연혁의 시행일 표시만 있는 경우일 수 있으니 law_addenda_search"
                            "(promul_no=공포번호)와 law_article_diff로 그날 실제로 바뀌는 조문을 확인하세요")
            versions.append(v)
        out["시행예정"] = versions
        if len(rows) > len(versions):
            out["안내"] = f"시행예정 {len(rows)}건 중 가까운 {len(versions)}건만 표시 — max_versions를 늘리세요"
        out["조회방법"] = ("개정 후 문안은 law_article_as_of(law_name, 시행일자, 조번호), "
                         "현행과의 대비는 law_article_diff(law_name, 조번호, 오늘, 시행일자)")
        return out

    # ---------- 별표·서식 ----------
    # 실응답(소득세법 시행령 MST 290841, 2026-11-20)의 <별표단위> 하위 태그. 가이드에 있던
    # '별표시행일자'는 실제로 없다 — 별표별 날짜는 본문 첫 줄의 '<개정 YYYY. M. D.>'뿐이다.
    _ANNEX_FIELDS = ("별표번호", "별표가지번호", "별표구분", "별표제목",
                     "별표서식파일링크", "별표HWP파일명", "별표서식PDF파일링크", "별표PDF파일명",
                     "별표내용")

    @classmethod
    def _parse_annex_units(cls, xml: str):
        """lawService target=law 응답의 <별표단위> 목록. '최종개정'은 본문 머리의
        '<개정 …>'·'<신설 …>' 표기에서 뽑는다 (별표 단위의 시행일 필드는 원문에 없음)."""
        units = []
        for m in re.finditer(r"<별표단위[^>]*>(.*?)</별표단위>", xml, re.S):
            blk = m.group(1)
            d = {}
            for tag in cls._ANNEX_FIELDS:
                fm = re.search(rf"<{tag}>(.*?)</{tag}>", blk, re.S)
                d[tag] = _strip_cdata(fm.group(1)) if fm else ""
            no = d["별표번호"].lstrip("0") or d["별표번호"]
            br = d["별표가지번호"].lstrip("0")
            kind = d["별표구분"] or "별표"
            body = _strip_html(d["별표내용"])
            body = body.replace("\r\n", "\n").replace("\r", "\n")
            body = re.sub(r"[ \t]+\n", "\n", body)
            body = re.sub(r"\n{2,}", "\n", body).strip()
            units.append({
                "번호": f"{kind} {no}" + (f"의{br}" if br else ""),
                "_no": no, "_br": br,
                "구분": kind,
                "제목": _strip_html(d["별표제목"]),
                "최종개정": _amend_note(body[:300]),
                "HWP": _abs_link(d["별표서식파일링크"]),
                "PDF": _abs_link(d["별표서식PDF파일링크"]),
                "_body": body,
            })
        return units

    def law_annexes(self, law_name: str = "", annex_no: str = "", keyword: str = "",
                    kind: str = "", law_id: str = "", as_of_date: str = "", mst: str = "",
                    max_chars: int = 8000):
        """한 법령의 별표·서식 목록 / 본문.
        - annex_no 없음: 목록 (번호·구분·제목·최종개정·HWP/PDF 링크). keyword로 제목·본문 필터
        - annex_no('1', '1의2', '별표 1의2'): 그 별표 본문 전문 + 원본 링크
        kind: '별표' 또는 '서식'으로 좁힘 (별표 1과 별지 제1호서식이 번호가 겹칠 수 있음)"""
        mst, meta = self._resolve_version(law_name, law_id, as_of_date, mst)
        xml = _get("lawService.do", target="law", MST=mst)
        name = re.search(r"<법령명_한글>(.*?)</법령명_한글>", xml, re.S)
        units = self._parse_annex_units(xml)
        out = {"법령명": _strip_cdata(name.group(1)) if name else meta.get("법령명", ""),
               "시행본": meta, "별표총수": len(units)}
        if not units:
            flag = re.search(r"<별표편집여부>(\w)</별표편집여부>", xml)
            out["status"] = "NOT_FOUND"
            out["_guidance"] = ("이 시행본 원문에 별표가 없음 (조회는 정상). 별표는 대개 시행령·시행규칙에 "
                                "있으니 law_name을 '○○법 시행령'으로 바꿔 보세요"
                                + (" — 원문 헤더는 별표 있음(Y)으로 표시돼 있어 응답 구조 확인 필요"
                                   if flag and flag.group(1) == "Y" else ""))
            return out
        if kind:
            units = [u for u in units if kind in u["구분"]] or units

        if annex_no:
            six = re.fullmatch(r"\s*(\d{4})(\d{2})\s*", annex_no)
            if six:  # 횡단 검색 결과의 별표번호 형식 '000304' → 3의4
                no, br = str(int(six.group(1))), (str(int(six.group(2))) if int(six.group(2)) else "")
            else:
                am = re.search(r"(\d+)(?:\s*의\s*(\d+))?", annex_no)
                if not am:
                    raise LawGoKrError(f"annex_no 형식 오류: '{annex_no}' (예: '1', '1의2', '000304')")
                no, br = am.group(1).lstrip("0") or "0", (am.group(2) or "").lstrip("0")
            hits = [u for u in units if u["_no"] == no and u["_br"] == br]
            if not kind and len({u["구분"] for u in hits}) > 1:
                pref = [u for u in hits if "별표" in u["구분"]]
                hits = pref or hits
            if not hits:
                raise LawGoKrNotFound(
                    f"{annex_no} 없음 — 이 시행본의 별표·서식은 {len(units)}건: "
                    + ", ".join(u["번호"] for u in units[:30]))
            u = hits[0]
            body = u["_body"]
            out["별표"] = {k: u[k] for k in ("번호", "구분", "제목", "최종개정", "HWP", "PDF")}
            out["별표"]["본문"] = body[:max_chars]
            out["별표"]["본문길이"] = len(body)
            if len(body) > max_chars:
                out["잘림"] = f"본문 {len(body)}자 중 앞 {max_chars}자만 표시 — max_chars를 늘려 재조회"
            if len(body) < 80:
                out["안내"] = ("텍스트 본문이 거의 없음 — 표·서식이 이미지로만 수록된 별표일 수 있으니 "
                              "HWP·PDF 링크로 원본을 확인하세요")
            else:
                out["안내"] = ("텍스트 표는 칸 정렬이 깨질 수 있음 — 세율·금액처럼 숫자가 결론을 좌우하면 "
                              "HWP·PDF 원본과 대조하세요")
            return out

        rows = units
        if keyword:
            terms = keyword.split()
            rows = [u for u in units if all(t in (u["제목"] + "\n" + u["_body"]) for t in terms)]
        out["목록"] = [{k: u[k] for k in ("번호", "구분", "제목", "최종개정", "HWP", "PDF")} for u in rows]
        if keyword:
            out["검색어"] = keyword
            if not rows:
                out["status"] = "NOT_FOUND"
                out["_guidance"] = "제목·본문에 검색어가 든 별표 없음 (조회는 정상) — 단어를 줄여 재검색"
        out["조회방법"] = "본문은 annex_no에 번호(예: '1', '1의2')를 지정해 다시 호출"
        return out

    def search_annex_files(self, keyword: str, search_scope: str = "title", kind: str = "",
                           display: int = 20, page: int = 1):
        """target=licbyl 법령 별표서식 목록 검색 (여러 법령에 걸쳐).
        search_scope: 'title'(별표서식명, 기본) | 'law'(해당 법령명) | 'body'(별표 본문)
        kind: '별표'·'서식'·'별지'·'별도'·'부록' (빈값=전체)
        응답 필드명은 open.law.go.kr 별표서식 목록 조회 가이드 기준. 반복 항목 태그는
        가이드에 명시가 없어 'licbyl'로 가정 — 건수는 있는데 항목이 0이면 PARSE_ERROR."""
        scope = {"title": 1, "law": 2, "body": 3}.get(search_scope)
        if not scope:
            raise LawGoKrError(f"search_scope는 title·law·body 중 하나: '{search_scope}'")
        knd = {"별표": 1, "서식": 2, "별지": 3, "별도": 4, "부록": 5}.get(kind, "") if kind else ""
        if kind and not knd:
            raise LawGoKrError(f"kind는 별표·서식·별지·별도·부록 중 하나: '{kind}'")
        xml = _get("lawSearch.do", target="licbyl", query=keyword, search=scope,
                   knd=knd, display=display, page=page)
        items = _parse_items(xml, "licbyl")
        total = re.search(r"<totalCnt>(\d+)</totalCnt>", xml)
        total_n = int(total.group(1)) if total else len(items)
        if total_n and not items:
            raise LawGoKrParseError(f"검색 건수 {total_n}건인데 항목을 해석하지 못함 — "
                                    f"응답 앞부분: {xml[:300]}")
        return {
            "검색어": keyword, "검색범위": search_scope, "총건수": total_n, "page": page,
            "결과": [{
                "별표일련번호": it.get("별표일련번호", ""),
                "별표명": _strip_tags(it.get("별표명", "")),
                "별표번호": it.get("별표번호", ""),
                "별표종류": it.get("별표종류", ""),
                "관련법령명": it.get("관련법령명", ""),
                "관련법령ID": it.get("관련법령ID", ""),
                "관련법령MST": it.get("관련법령일련번호", ""),
                "공포일자": it.get("공포일자", ""),
                "HWP": _abs_link(it.get("별표서식파일링크", "")),
                "PDF": _abs_link(it.get("별표서식PDF파일링크", "")),
            } for it in items],
            "조회방법": "본문은 law_annex_search(mst=관련법령MST, annex_no=별표번호)",
        }

    # ---------- 편의: 특정 날짜 시행본의 조문 (예규 당시 조문 확인용) ----------
    def law_article_as_of(self, law_name: str, as_of_date: str, article_no: str, law_id: str = "",
                          max_chars: int = 6000):
        """as_of_date(YYYYMMDD) 당시 시행 중이던 시행본을 골라 해당 조문 원문 반환.
        예규·판례가 인용한 '당시 조문' 검증용 — 회신일을 넣으면 그 시점 법을 준다."""
        if not re.match(r"^\d{8}$", str(as_of_date or "")):
            raise LawGoKrError(f"as_of_date 형식 오류: '{as_of_date}' (YYYYMMDD 8자리)")
        history = self.law_history(law_name, law_id=law_id)
        if not history:
            raise LawGoKrNotFound(f"연혁 없음: {law_name}")
        # 시행본 시행일자만 보면 조문별 단계 시행에서 아직 발효 전인 문안을 집는다
        chosen, _lines, art_date = self._pick_effective_version(history, as_of_date, article_no)
        if not chosen:
            raise LawGoKrNotFound(f"{as_of_date} 이전 시행본 없음 (최초 시행 {history[0]['시행일자']})")
        art = self.law_article(chosen["MST"], article_no, max_chars)
        art["적용시행본"] = {k: chosen[k] for k in ("법령명", "시행일자", "공포일자", "공포번호", "제개정구분", "MST")}
        if art_date and art_date > as_of_date:
            art["주의"] = (f"이 시행본에 수록된 문안의 조문시행일자({art_date})가 기준일"
                          f"({as_of_date})보다 미래입니다 — 기준일 당시 시행 중이던 문안을 "
                          f"찾지 못했을 수 있으니 law_article_diff로 개정 시점을 확인하세요")
        return art

    # ---------- 조문 개정 diff (신구조문 대비) ----------
    _CONTENT_TAG_RE = re.compile(r"<(조문내용|항내용|호내용|목내용)>(.*?)</\1>", re.S)
    _BACKTRACK_MAX = 8  # 단계 시행 소급 탐색 상한 (시행본 XML 재요청 왕복 제한)

    def _article_state(self, mst: str, article_no: str):
        """(문안 줄 목록 or None, 조문시행일자) — 조문이 없으면 (None, "")."""
        xml = _get("lawService.do", target="law", MST=mst)
        _label, block = self._find_article_block(xml, article_no)
        if block is None:
            return None, ""
        em = re.search(r"<조문시행일자>(\d+)</조문시행일자>", block)
        return self._article_content_lines(block), (em.group(1) if em else "")

    def _pick_effective_version(self, history, date: str, article_no: str, state_fn=None):
        """기준일에 '실제로 시행 중이던' 조문 문안을 담은 시행본을 고른다.

        시행본 시행일자만 보고 고르면 조문별 단계 시행에서 아직 발효 전인 문안을
        집는다 — 같은 공포본(MST)이 여러 시행일자 행으로 연혁에 나타나고, 그 MST의
        XML은 미래 시행 조문의 문안까지 담고 있기 때문(2026-08-28 실측: 소득세법
        현행본 헤더 <조문시행일자문자열> = "20260701:제57조의2 … 20270101:제17조제3항 …").
        후보의 조문시행일자가 기준일보다 미래면 이전 시행본으로 소급한다.

        반환: (선택된 연혁 행, 문안 줄 목록 or None, 조문시행일자). 후보가 없으면
        (None, None, ""). 조문 자체가 없는 시점이면 문안이 None (= 신설 전)."""
        get_state = state_fn or (lambda mst: self._article_state(mst, article_no))
        cands = [r for r in history if r.get("시행일자") and r["시행일자"] <= date]
        if not cands:
            return None, None, ""
        tried = set()
        for row in reversed(cands[-self._BACKTRACK_MAX:]):
            if row["MST"] in tried:  # 같은 MST의 중복 시행일자 행은 한 번만 조회
                continue
            tried.add(row["MST"])
            lines, art_date = get_state(row["MST"])
            if lines is None:
                return row, None, ""  # 그 시점엔 조문 없음 (신설 전) — 소급 불필요
            if art_date and art_date > date:
                continue  # 아직 발효 전 문안 — 더 과거 시행본으로
            return row, lines, art_date
        row = cands[0]  # 소급 상한 내 전부 미래 문안 (비정상) — 최고(最古) 후보로 폴백
        lines, art_date = get_state(row["MST"])
        return row, lines, art_date

    @classmethod
    def _article_content_lines(cls, block: str):
        """diff 비교용: 조문 블록에서 실제 문안(조문내용·항·호·목)만 줄 단위로 추출.
        <조문키>·<조문시행일자> 같은 메타데이터와 번호 태그 중복(①/① 본문)을 제외해야
        문안이 같은데 메타데이터만 다른 시행본이 '변경'으로 오판되지 않는다."""
        lines = []
        for _tag, val in cls._CONTENT_TAG_RE.findall(block):
            t = _strip_cdata(val)
            # "<개정 2024.12.31>" 류 주석이 태그 제거에 지워지지 않게 보존 — 개정 이력 자체가 유용한 정보
            t = re.sub(r"<((?:개정|신설|삭제)[^>]*?)>", r"〈\1〉", t)
            t = _strip_tags(t)
            for ln in t.splitlines():
                ln = ln.strip()
                if ln:
                    lines.append(ln)
        return lines

    def law_article_diff(self, law_name: str, article_no: str, date_from: str,
                         date_to: str = "", law_id: str = "", max_chars: int = 8000,
                         find_change: bool = True):
        """두 시점의 조문 문안을 비교(신구조문 대비)하고, 현행(B) 문안이 언제부터
        시행됐는지와 그 개정의 부칙(시행일·적용례)까지 연결해 반환한다.

        - date_from(A)·date_to(B, 생략 시 오늘) 각각의 시행본을 자동 선택해 조문을 대비
        - find_change=True면 A~B 사이 시행본을 이진탐색해 B 문안이 최초로 등장한
          시행본을 특정하고, 그 개정 부칙에서 이 조문이 언급된 적용례를 발췌
        """
        if not date_from:
            raise LawGoKrError("date_from은 필수입니다 (YYYYMMDD 8자리)")
        for d, nm in ((date_from, "date_from"), (date_to, "date_to")):
            if d and not re.match(r"^\d{8}$", str(d)):
                raise LawGoKrError(f"{nm} 형식 오류: '{d}' (YYYYMMDD 8자리)")
        if not date_to:
            date_to = time.strftime("%Y%m%d")
        if date_from >= date_to:
            raise LawGoKrError(f"date_from({date_from})은 date_to({date_to})보다 앞선 날짜여야 합니다")

        history = self.law_history(law_name, law_id=law_id)
        if not history:
            raise LawGoKrNotFound(f"연혁 없음: {law_name}")

        state_cache = {}  # MST -> (문안 줄 목록 or None, 조문시행일자)

        def state(mst):
            if mst not in state_cache:
                state_cache[mst] = self._article_state(mst, article_no)
            return state_cache[mst]

        # 시행본 시행일자만이 아니라 그 조문의 조문시행일자까지 보고 고른다
        # (조문별 단계 시행이면 시행본 XML에 아직 발효 전인 문안이 들어 있음)
        va, lines_a, art_date_a = self._pick_effective_version(history, date_from, article_no, state)
        if not va:
            raise LawGoKrNotFound(f"{date_from} 이전 시행본 없음 (최초 시행 {history[0]['시행일자']})")
        vb, lines_b, art_date_b = self._pick_effective_version(history, date_to, article_no, state)
        if not vb:
            raise LawGoKrNotFound(f"{date_to} 이전 시행본 없음: {law_name}")

        def meta(v):
            return {k: v[k] for k in ("법령명", "시행일자", "공포일자", "공포번호", "제개정구분", "MST")}

        label, _ = self._find_article_block("", article_no)  # 라벨 문자열만 계산
        out = {
            "법령명": va["법령명"],
            "조문": label,
            "비교기준": {
                "A": {"기준일": date_from, "시행본": meta(va)},
                "B": {"기준일": date_to, "시행본": meta(vb)},
            },
        }
        if art_date_a:
            out["비교기준"]["A"]["조문시행일자"] = art_date_a
        if art_date_b:
            out["비교기준"]["B"]["조문시행일자"] = art_date_b
        if va["MST"] == vb["MST"]:
            out["변경여부"] = False
            out["안내"] = ("두 기준일에 같은 조문 문안이 시행 중이었음 — "
                          "이 구간에 이 조문의 개정 시행이 없었음")
            return out
        if lines_a is None and lines_b is None:
            raise LawGoKrNotFound(f"{label}: A·B 두 시점 모두 조문이 존재하지 않음 (조번호 확인)")
        if lines_a == lines_b:
            out["변경여부"] = False
            out["안내"] = ("시행본은 다르지만 이 조문의 문안은 동일 — "
                          "이 구간의 개정은 이 조문을 건드리지 않았음")
            return out

        out["변경여부"] = True
        if lines_a is None:
            out["변경유형"] = "신설 (A 시점에는 없던 조문)"
        elif lines_b is None:
            out["변경유형"] = "삭제 (B 시점에는 없는 조문)"
        else:
            out["변경유형"] = "개정"
        diff_text = "\n".join(difflib.unified_diff(
            lines_a or [], lines_b or [],
            fromfile=f"A ({va['시행일자']} 시행본)", tofile=f"B ({vb['시행일자']} 시행본)",
            lineterm="", n=1))
        out["diff"] = diff_text[:max_chars]
        if len(diff_text) > max_chars:
            out["잘림"] = f"diff 전체 {len(diff_text)}자 중 앞 {max_chars}자만 표시 — max_chars를 늘려 재조회"

        if find_change:
            between = [r for r in history
                       if va["시행일자"] < (r["시행일자"] or "") <= vb["시행일자"]]
            if between:
                # B 문안은 어느 시점부터 계속 유지됐다고 보고(중간 되돌림 없음 가정),
                # B 문안과 같은 상태가 시작되는 최초 시행본을 이진탐색 — 왕복 log2(N)회
                lo, hi = 0, len(between) - 1
                while lo < hi:
                    mid = (lo + hi) // 2
                    if state(between[mid]["MST"])[0] == lines_b:
                        hi = mid
                    else:
                        lo = mid + 1
                change = between[lo]
                out["현행_문안_시작"] = meta(change)
                # 조문별 단계 시행이면 같은 공포본(MST)이 여러 시행일자 행으로 나타나므로,
                # 이 조문의 실제 적용 시작일은 시행본 시행일자가 아니라 조문시행일자가 기준
                ch_art_date = state(change["MST"])[1]
                if ch_art_date:
                    out["현행_문안_시작"]["이_조문의_시행일자"] = ch_art_date
                    if ch_art_date != change.get("시행일자"):
                        out["현행_문안_시작"]["주의"] = (
                            "조문별 단계 시행 — 이 조문의 실제 적용 시작은 "
                            f"'이_조문의_시행일자'({ch_art_date}) 기준")
                out["개정_부칙"] = self._addendum_for_change(change, label)
                out["안내"] = ("'현행_문안_시작'은 B 문안이 처음 수록된 시행본입니다. "
                              "A~B 사이에 개정이 여러 번 있었는지 확인하려면 date_to를 "
                              f"{change['시행일자']} 이전으로 좁혀 다시 비교하세요.")
        return out

    def _addendum_for_change(self, change_row: dict, label: str):
        """변경이 일어난 시행본의 부칙에서 시행일 요약 + 해당 조문 언급 항을 발췌."""
        xml = _get("lawService.do", target="law", MST=change_row["MST"])
        units = self._parse_addenda_units(xml)
        want = (change_row.get("공포번호") or "").lstrip("0")
        unit = next((u for u in units if u["공포번호"] == want), None)
        if unit is None:
            return {"안내": (f"공포번호 {change_row.get('공포번호')}의 부칙을 찾지 못함 "
                            f"(타법개정 등) — law_addenda_search로 직접 조회")}
        adx = {
            "공포일자": unit["공포일자"],
            "공포번호": unit["공포번호"],
            "시행일_요약": self._effective_summary(unit["본문"]),
        }
        # '제96조' 검색이 '제96조의2' 언급에 오매치되지 않게 가지조문 아니면 (?!의) 가드
        pat = re.compile(re.escape(label) + ("" if "의" in label else r"(?!의)"))
        paras = [p[:1500] for p in self._split_addendum(unit["본문"]) if pat.search(p)]
        if paras:
            adx["이_조문_언급_항"] = paras[:5]
        else:
            adx["안내"] = ("이 개정 부칙에 이 조문을 명시한 적용례·경과조치 없음 — "
                          "시행일_요약의 일반 시행일을 따름")
        return adx
