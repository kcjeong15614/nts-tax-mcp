# -*- coding: utf-8 -*-
"""
test_new_tools.py — v5.6 추가 도구 회귀 테스트 (오프라인)

- law_article_search, law_upcoming: 실제 캡처 픽스처(소득세법 MST 280405, eflaw 연혁)로 검증
- law_annexes, get_treaty(article): 실제 응답 픽스처가 아직 없어 open.law.go.kr 가이드의
  필드명으로 만든 합성 XML로 파서만 검증한다. 실서버 응답으로 한 번 확인한 뒤
  refresh_fixtures.py에 별표 있는 시행령·조세조약 캡처를 추가할 것.

실행: python tests/test_new_tools.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from test_parsers import FIXTURES, read_gz, manifest  # noqa: E402

import law_go_kr as L  # noqa: E402


class _patched_get:
    """law_go_kr._get을 주어진 함수로 바꿔치기."""
    def __init__(self, fn):
        self.fn = fn

    def __enter__(self):
        self.orig = L._get
        L._get = self.fn
        return L.LawGoKrClient()

    def __exit__(self, *exc):
        L._get = self.orig
        return False


def _fixture_get(endpoint, **params):
    """test_parsers._fixture_get과 같되, 픽스처에 없는 MST는 NotFound로 응답
    (law_upcoming이 행별 오류로 처리하는지 확인하기 위해)."""
    target = params.get("target")
    if target == "eflaw":
        path = FIXTURES / f"law_eflaw_p{params.get('page', 1)}.xml.gz"
        return read_gz(path.name) if path.exists() else "<법령검색></법령검색>"
    if target == "law" and params.get("MST"):
        path = FIXTURES / f"law_{params['MST']}.xml.gz"
        if path.exists():
            return read_gz(path.name)
        raise L.LawGoKrAuthError(f"픽스처 없음 MST {params['MST']}")
    if target == "law":  # search_laws (현행 검색) — 현행본 하나만 돌려준다
        m = manifest()["law"]
        return (f"<LawSearch><law id=\"1\"><법령일련번호>{m['mst_current']}</법령일련번호>"
                f"<법령명한글><![CDATA[소득세법]]></법령명한글><법령ID>{m['law_id']}</법령ID>"
                f"<시행일자>{m['current_시행일자']}</시행일자></law></LawSearch>")
    raise AssertionError(f"픽스처에 없는 요청: {endpoint} {params}")


# ---------------------------------------------------------------------------
# 조문 키워드 검색
# ---------------------------------------------------------------------------

def test_iter_articles_skips_headings_and_addenda():
    xml = read_gz("law_280405.xml.gz")
    arts = L.LawGoKrClient._iter_articles(xml)
    assert len(arts) == 326, f"조문여부=조문 블록 수와 달라야 할 이유 없음: {len(arts)}"
    labels = [a["조문"] for a in arts]
    assert "제94조" in labels and "제57조의2" in labels
    assert all(not a["조문"].startswith("제0조") for a in arts)
    a94 = next(a for a in arts if a["조문"] == "제94조")
    assert a94["조문제목"] == "양도소득의 범위", a94["조문제목"]


def test_law_article_search_finds_and_ranks_title_hits():
    with _patched_get(_fixture_get) as c:
        r = c.law_article_search("소득세법", "양도소득", law_id="001565")
    assert r["일치조문수"] > 5, r["일치조문수"]
    assert r["결과"][0]["조문제목"] and "양도소득" in r["결과"][0]["조문제목"], r["결과"][0]
    assert r["시행본"]["MST"] == manifest()["law"]["mst_current"]
    first = r["결과"][0]
    assert "_title_hit" not in first and first["발췌"]


def test_law_article_search_and_semantics_and_not_found():
    with _patched_get(_fixture_get) as c:
        one = c.law_article_search("소득세법", "조정대상지역")
        both = c.law_article_search("소득세법", "조정대상지역 1세대")
        none = c.law_article_search("소득세법", "zzqq없는단어xx")
    assert one["일치조문수"] >= both["일치조문수"] >= 1, (one["일치조문수"], both["일치조문수"])
    assert none["status"] == "NOT_FOUND" and none["결과"] == []


def test_law_article_search_as_of_uses_history():
    m = manifest()["law"]
    with _patched_get(_fixture_get) as c:
        r = c.law_article_search("소득세법", "양도소득", law_id=m["law_id"], as_of_date=m["old_date"])
    assert r["시행본"]["MST"] == m["mst_old"], r["시행본"]


# ---------------------------------------------------------------------------
# 시행 예정 개정
# ---------------------------------------------------------------------------

def test_staged_items_parses_addendum_article1():
    xml = read_gz("law_280405.xml.gz")
    unit = next(u for u in L.LawGoKrClient._parse_addenda_units(xml) if u["공포번호"] == "21221")
    items = L.LawGoKrClient._staged_items(unit["본문"])
    dates = [d for d, _ in items]
    assert dates == ["20260701", "20270101"], items
    assert "제118조의9" in items[1][1]


def test_korean_date():
    assert L._korean_date_to_ymd("2027년 1월 1일") == "20270101"
    assert L._korean_date_to_ymd(": 2026년 7월 1일") == "20260701"
    assert L._korean_date_to_ymd("없음") == ""


def test_law_upcoming_staged_enforcement():
    """캡처일(2026-08-28) 기준: 280405(제21221호)의 2027-01-01 시행분은 부칙 단서 2호."""
    with _patched_get(_fixture_get) as c:
        r = c.law_upcoming("소득세법", law_id="001565", max_versions=10, today="20260828")
    assert r["시행예정_건수"] > 0
    v = next(v for v in r["시행예정"] if v["MST"] == "280405" and v["시행일자"] == "20270101")
    assert v["근거"].startswith("부칙 제1조 단서"), v
    labels = [a["조문"] for a in v["언급_조문"]]
    assert labels[:2] == ["제17조", "제118조의9"], labels
    # '제118조의9부터 제118조의18까지' 범위가 원문 순서대로 펼쳐져야 한다
    assert "제118조의12" in labels and labels[-1] == "제118조의18", labels
    assert len(labels) == 11, labels
    assert any(a["조문제목"] for a in v["언급_조문"]), "조문제목이 원문에서 채워져야 함"
    # 픽스처 없는 MST 행은 전체를 실패시키지 않고 행별 오류로 남는다
    others = [v for v in r["시행예정"] if v["MST"] != "280405"]
    assert others and all("오류" in v for v in others)


def test_law_upcoming_none():
    with _patched_get(_fixture_get) as c:
        r = c.law_upcoming("소득세법", law_id="001565", today="20991231")
    assert r["status"] == "NOT_FOUND" and r["시행예정_건수"] == 0


def test_law_upcoming_base_date_lists_changed_articles():
    """본칙 시행일과 같은 날이면 <조문변경여부>Y 조문을, 단서 호의 조문은 빼고 보여준다."""
    hist = [{"법령명": "소득세법", "법령ID": "001565", "MST": "280405", "시행일자": "20260101",
             "공포일자": "20251223", "공포번호": "21221", "제개정구분": "일부개정"}]
    with _patched_get(_fixture_get) as c:
        c.law_history = lambda *a, **k: hist
        r = c.law_upcoming("소득세법", today="20251231")
    v = r["시행예정"][0]
    assert v["근거"].startswith("본칙 시행일"), v
    labels = {a["조문"] for a in v["개정_조문"]}
    assert labels and "제118조의9" not in labels and "제17조" not in labels, labels


# ---------------------------------------------------------------------------
# 별표 (합성 XML — 필드명은 open.law.go.kr 현행법령 본문 조회 가이드 기준)
# ---------------------------------------------------------------------------

_ANNEX_XML = """<?xml version="1.0" encoding="UTF-8"?><법령><기본정보>
<법령명_한글><![CDATA[테스트법 시행령]]></법령명_한글><시행일자>20260101</시행일자>
<별표편집여부>Y</별표편집여부></기본정보>
<조문><조문단위 조문키="0001001"><조문번호>1</조문번호><조문여부>조문</조문여부>
<조문내용><![CDATA[제1조(목적) 테스트]]></조문내용></조문단위></조문>
<별표><별표단위 별표키="000100">
<별표번호>0001</별표번호><별표가지번호>00</별표가지번호><별표구분>별표</별표구분>
<별표제목><![CDATA[업종별 기준경비율]]></별표제목><별표시행일자>20260101</별표시행일자>
<별표서식파일링크>/LSW/flDownload.do?flSeq=111</별표서식파일링크>
<별표HWP파일명>a.hwp</별표HWP파일명>
<별표서식PDF파일링크>/LSW/flDownload.do?flSeq=112</별표서식PDF파일링크>
<별표내용><![CDATA[■ 테스트법 시행령 [별표 1]
업종별 기준경비율(제1조 관련)
구분   경비율
제조업   20%
도매업   10%
비고: 이 표는 테스트용이다. 숫자가 결론을 좌우하면 원본과 대조한다. 충분히 긴 본문을 만들기 위한 문장.]]></별표내용>
</별표단위><별표단위 별표키="000102">
<별표번호>0001</별표번호><별표가지번호>02</별표가지번호><별표구분>별표</별표구분>
<별표제목><![CDATA[이미지만 있는 별표]]></별표제목>
<별표서식파일링크>/LSW/flDownload.do?flSeq=121</별표서식파일링크>
<별표내용><![CDATA[]]></별표내용></별표단위>
<별표단위 별표키="000110"><별표번호>0001</별표번호><별표가지번호>00</별표가지번호>
<별표구분>서식</별표구분><별표제목><![CDATA[신청서]]></별표제목>
<별표서식파일링크>/LSW/flDownload.do?flSeq=131</별표서식파일링크><별표내용><![CDATA[신청서 양식]]></별표내용>
</별표단위></별표>
<부칙><부칙단위><부칙공포일자>20251231</부칙공포일자><부칙공포번호>00001</부칙공포번호>
<부칙내용><![CDATA[부칙 <제1호,2025.12.31>
이 영은 2026년 1월 1일부터 시행한다.]]></부칙내용></부칙단위></부칙></법령>"""


def _annex_get(endpoint, **params):
    if params.get("target") == "law" and params.get("MST") == "999":
        return _ANNEX_XML
    raise AssertionError(f"예상 밖 요청: {params}")


def test_annex_list_and_links():
    with _patched_get(_annex_get) as c:
        r = c.law_annexes(mst="999")
    assert r["별표총수"] == 3
    nos = [x["번호"] for x in r["목록"]]
    assert nos == ["별표 1", "별표 1의2", "서식 1"], nos
    assert r["목록"][0]["HWP"] == "https://www.law.go.kr/LSW/flDownload.do?flSeq=111"
    assert r["목록"][0]["PDF"].endswith("flSeq=112")
    assert "_body" not in r["목록"][0]


def test_annex_body_prefers_byeolpyo_and_branch():
    with _patched_get(_annex_get) as c:
        one = c.law_annexes(mst="999", annex_no="1")
        branch = c.law_annexes(mst="999", annex_no="별표 1의2")
        form = c.law_annexes(mst="999", annex_no="1", kind="서식")
    assert one["별표"]["제목"] == "업종별 기준경비율" and "제조업" in one["별표"]["본문"]
    assert "칸 정렬" in one["안내"]
    assert branch["별표"]["번호"] == "별표 1의2" and "이미지" in branch["안내"]
    assert form["별표"]["구분"] == "서식"


def test_annex_keyword_filter_and_missing():
    with _patched_get(_annex_get) as c:
        r = c.law_annexes(mst="999", keyword="도매업")
        try:
            c.law_annexes(mst="999", annex_no="7")
            raise AssertionError("없는 별표는 NotFound여야 함")
        except L.LawGoKrNotFound as e:
            assert "별표 1" in str(e)
    assert [x["번호"] for x in r["목록"]] == ["별표 1"]


def test_annex_none_in_statute_is_not_found_with_hint():
    with _patched_get(_fixture_get) as c:
        r = c.law_annexes("소득세법", law_id="001565")
    assert r["status"] == "NOT_FOUND" and "시행령" in r["_guidance"]


def test_annex_cross_search_parse_error_when_items_missing():
    def g(endpoint, **params):
        return "<LicBylSearch><totalCnt>5</totalCnt><unknown id='1'/></LicBylSearch>"
    with _patched_get(g) as c:
        try:
            c.search_annex_files("기준경비율")
            raise AssertionError("건수는 있는데 항목이 없으면 ParseError")
        except L.LawGoKrParseError:
            pass


def test_annex_cross_search_parses_licbyl():
    def g(endpoint, **params):
        assert params["target"] == "licbyl" and params["search"] == 3 and params["knd"] == 1
        return ("<LicBylSearch><totalCnt>1</totalCnt><licbyl id=\"1\"><별표일련번호>77</별표일련번호>"
                "<별표명><![CDATA[기준경비율]]></별표명><관련법령명><![CDATA[소득세법 시행규칙]]></관련법령명>"
                "<관련법령일련번호>555</관련법령일련번호><별표번호>0003</별표번호>"
                "<별표서식파일링크>/LSW/flDownload.do?flSeq=9</별표서식파일링크></licbyl></LicBylSearch>")
    with _patched_get(g) as c:
        r = c.search_annex_files("기준경비율", search_scope="body", kind="별표")
    it = r["결과"][0]
    assert it["관련법령MST"] == "555" and it["HWP"].startswith("https://www.law.go.kr/")


# ---------------------------------------------------------------------------
# 조세조약 조문 단위 (합성 XML — 필드명은 open.law.go.kr 조약 본문 조회 가이드 기준)
# ---------------------------------------------------------------------------

_TREATY_KO = """<?xml version="1.0" encoding="UTF-8"?><조약>
<조약명_한글><![CDATA[대한민국 정부와 테스트국 정부 간의 소득에 대한 이중과세회피협약]]></조약명_한글>
<발효일자>20000101</발효일자><서명일자>19990101</서명일자><체결대상국가한글>테스트국</체결대상국가한글>
<조약내용><![CDATA[<p>제1조 인적 범위</p><p>이 협약은 일방 또는 양 체약국의 거주자인 인에게 적용된다.</p>
<p>제10조 배당</p><p>1. 일방체약국의 거주자인 법인이 지급하는 배당은 그 타방국에서 과세될 수 있다.</p>
<p>2. 그러나 제10조에 따른 배당은 총액의 15퍼센트를 초과하지 아니한다.</p>
<p>제10조의2 지점세</p><p>지점 이윤에 대한 추가 과세.</p>
<p>제11조 이자</p><p>이자는 10퍼센트를 초과하지 아니한다.</p>
<p>의정서</p><p>제10조</p><p>의정서상 배당 조항의 해석.</p>]]></조약내용></조약>"""

_TREATY_EN = """<조약><조약명_한글><![CDATA[테스트 협약]]></조약명_한글><조약내용><![CDATA[
Article 1<br/>Persons Covered<br/>This Convention shall apply to persons.<br/>
Article 10<br/>Dividends<br/>Dividends may be taxed. The tax shall not exceed 15 per cent as provided in Article 10.<br/>
ARTICLE 11<br/>Interest<br/>Interest shall not exceed 10 per cent.<br/>]]></조약내용></조약>"""


def _treaty_get(endpoint, **params):
    assert params["target"] == "trty"
    return _TREATY_EN if params.get("chrClsCd") == "010203" else _TREATY_KO


def test_treaty_meta_title_fixed():
    with _patched_get(_treaty_get) as c:
        r = c.get_treaty("1")
    assert r["조약명"].startswith("대한민국 정부와 테스트국"), r["조약명"]
    assert r["발효일자"] == "20000101" and r["체결대상국가한글"] == "테스트국"
    assert "<p>" not in r["본문"] and "제10조 배당" in r["본문"]


def test_treaty_article_slice_ko():
    with _patched_get(_treaty_get) as c:
        r10 = c.get_treaty("1", article="10")
        r10b = c.get_treaty("1", article="10의2")
        r10p = c.get_treaty("1", article="10", occurrence=2)
    seg = r10["본문"]
    assert seg.startswith("제10조 배당") and "15퍼센트" in seg, seg
    assert "지점세" not in seg and "이자" not in seg, "다음 조 표제에서 끊겨야 함"
    assert "2번" in r10["안내"], "의정서의 같은 조번호 존재를 알려야 함"
    assert r10b["본문"].startswith("제10조의2 지점세")
    assert "의정서상" in r10p["본문"]


def test_treaty_article_slice_en_and_errors():
    with _patched_get(_treaty_get) as c:
        en = c.get_treaty("1", article="10", lang="en")
        en11 = c.get_treaty("1", article="11", lang="en")
        miss = c.get_treaty("1", article="29")
        for bad in (dict(lang="jp"), dict(article="열"), dict(article="10", occurrence=5)):
            try:
                c.get_treaty("1", **bad)
                raise AssertionError(f"오류여야 함: {bad}")
            except L.LawGoKrError:
                pass
    assert en["언어"] == "en" and en["본문"].startswith("Article 10") and "15 per cent" in en["본문"]
    assert "Interest" not in en["본문"]
    assert en11["본문"].startswith("ARTICLE 11")
    assert "오류" in miss


def test_treaty_window_reading():
    with _patched_get(_treaty_get) as c:
        r = c.get_treaty("1", max_chars=40)
        r2 = c.get_treaty("1", max_chars=40, start_char=40)
    assert len(r["본문"]) == 40 and "start_char=40" in r["잘림"]
    assert r2["본문"] and r2["본문"] != r["본문"]


# ---------------------------------------------------------------------------
# MCP 등록 · status 계약
# ---------------------------------------------------------------------------

def test_server_ext_registers_21_tools():
    import asyncio
    import server_ext
    tools = asyncio.run(server_ext.mcp.list_tools())
    names = {t.name for t in tools}
    for n in ("law_article_search", "law_upcoming_changes", "law_annex_search", "treaty_search",
              "law_article_as_of", "nts_ruling_get_full_text", "verify_citations"):
        assert n in names, f"도구 누락: {n}"
    assert len(names) == 21, sorted(names)
    tr = next(t for t in tools if t.name == "treaty_search")
    for p in ("article", "lang", "start_char", "occurrence"):
        assert p in tr.inputSchema["properties"], p


def test_safe_maps_parse_error():
    import server_ext

    def boom():
        raise L.LawGoKrParseError("x")
    assert server_ext._safe(boom)["status"] == "PARSE_ERROR"


def test_annex_tool_requires_input():
    import server_ext
    assert server_ext.law_annex_search()["status"] == "INVALID_INPUT"
    assert server_ext.treaty_search()["status"] == "INVALID_INPUT"


def test_check_response_flags_non_result_pages():
    """정상 결과가 아닌 응답을 0건(NOT_FOUND)으로 오인하지 않아야 한다."""
    bad = [
        ("lawSearch.do", "사용자 정보 검증에 실패하였습니다. OPEN API 신청 정보를 확인하세요."),
        ("lawSearch.do", "<html><body>미신청된 목록/본문에 대한 접근입니다.</body></html>"),
        ("lawSearch.do", ""),
        ("lawService.do", "<!DOCTYPE html><html><body>오류</body></html>"),
        ("lawService.do", "등록되지 않은 IP입니다"),
    ]
    for ep, text in bad:
        try:
            L._check_response(ep, text)
            raise AssertionError(f"걸러야 함: {ep} {text[:30]}")
        except L.LawGoKrAuthError as e:
            assert "응답 내용" in str(e)
    # 정상 응답(0건 포함)은 통과
    L._check_response("lawSearch.do", "<LawSearch><totalCnt>0</totalCnt></LawSearch>")
    L._check_response("lawSearch.do", read_gz("law_eflaw_p1.xml.gz"))
    L._check_response("lawService.do", read_gz("law_280405.xml.gz"))
    L._check_response("lawService.do", "﻿<?xml version='1.0'?><조약></조약>")


def test_get_reports_auth_error_through_safe():
    """_get 경로 전체: 안내문 응답 → AUTH_ERROR(응답 문구 포함), 캐시에 남지 않음."""
    import server_ext

    class R:
        status_code = 200
        text = "사용자 정보 검증에 실패하였습니다."

        def raise_for_status(self):
            pass
    orig_get, orig_oc = L.requests.get, L.OC
    L.requests.get = lambda *a, **k: R()
    L.OC = "test"
    L._cache.clear()
    try:
        out = server_ext._safe(L.LawGoKrClient().search_laws, "소득세법")
    finally:
        L.requests.get, L.OC = orig_get, orig_oc
    assert out["status"] == "AUTH_ERROR", out
    assert "검증에 실패" in out["오류"], out
    assert not L._cache, "잘못된 응답이 캐시되면 안 됨"


def test_base_is_https():
    assert L.BASE.startswith("https://"), L.BASE


def _run_all():
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
