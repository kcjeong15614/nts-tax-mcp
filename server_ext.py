# -*- coding: utf-8 -*-
"""
server_ext.py — nts-tax-mcp 확장 진입점
기존 server.py의 도구 8개(국세 NTS + 붙임 전문 + 지방세 olta + 인용검증)를 그대로 물려받고,
법제처 law.go.kr Open API 도구 13개를 추가한다. 커넥터 하나로 통합 운영 (총 21개).

실행: PORT=8734 LAW_API_OC=<발급받은 기관코드> python server_ext.py
(run_server.bat이 이 파일을 실행한다. server.py는 수정하지 않는다.)
"""
import logging
from typing import Optional

import requests

from server import mcp  # 기존 FastMCP 인스턴스 + 도구 그대로 재사용
from law_go_kr import (LawGoKrClient, LawGoKrError, LawGoKrAuthError, LawGoKrNotFound,
                       LawGoKrParseError)

logger = logging.getLogger("nts-tax-mcp.ext")
_law = LawGoKrClient()

# status 계약 (server.py와 동일한 규칙):
#   OK          정상 — 결과 있음
#   NOT_FOUND   조회는 성공했으나 해당 자료가 원천에 없음 (부존재로 판단 가능)
#   AUTH_ERROR  law.go.kr 인증 실패 (OC 미설정·IP 미등록) — 부존재와 무관
#   UPSTREAM_ERROR  원천 접속 실패 — 부존재로 단정 금지
#   PARSE_ERROR     응답 구조가 예상과 다름(원천 개편 가능성) — 부존재로 단정 금지
#   INVALID_INPUT   입력 형식 오류
_UPSTREAM_GUIDE = (
    "law.go.kr 접속에 실패했습니다. 자료가 '없다'는 뜻이 아니므로 부존재로 단정하지 말고 "
    "잠시 후 재시도하세요."
)


def _safe(fn, *args, **kwargs):
    try:
        out = fn(*args, **kwargs)
    except LawGoKrAuthError as e:
        return {"status": "AUTH_ERROR", "오류": str(e),
                "_guidance": "인증 문제이므로 자료 부존재로 해석하지 말 것 — 서버 관리자 확인 필요"}
    except LawGoKrNotFound as e:
        return {"status": "NOT_FOUND", "오류": str(e),
                "_guidance": "조회는 정상 수행됐고 해당 자료가 원천에 없음 — 입력(법령명·번호·형식·기준일)을 확인"}
    except LawGoKrParseError as e:
        logger.error("law.go.kr 응답 해석 실패: %s", e)
        return {"status": "PARSE_ERROR", "오류": str(e),
                "_guidance": "응답 구조가 예상과 다름 — 자료 부존재로 단정하지 말 것 (파서 점검 필요)"}
    except LawGoKrError as e:
        return {"status": "INVALID_INPUT", "오류": str(e)}
    except requests.exceptions.RequestException as e:
        logger.error("law.go.kr 접속 실패: %s", e)
        return {"status": "UPSTREAM_ERROR", "오류": f"{type(e).__name__}: {e}",
                "_guidance": _UPSTREAM_GUIDE}
    except Exception as e:  # noqa: BLE001
        logger.exception("law.go.kr 호출 실패")
        return {"status": "UPSTREAM_ERROR", "오류": f"{type(e).__name__}: {e}",
                "_guidance": _UPSTREAM_GUIDE}
    # 성공 경로 — 응답 형태를 status 계약에 맞춘다
    if isinstance(out, list):
        if not out:
            return {"status": "NOT_FOUND", "결과": [],
                    "_guidance": "검색 결과 0건 (검색 자체는 정상) — 검색어를 바꿔 재시도"}
        return {"status": "OK", "결과": out}
    if isinstance(out, dict):
        if "status" not in out:
            # 일부 조회 함수는 실패를 {"오류": …}로 반환한다 — NOT_FOUND로 분류
            out["status"] = "NOT_FOUND" if "오류" in out else "OK"
        return out
    return out


@mcp.tool()
def court_case_search(keyword: str, court: str = "", date_from: str = "",
                      date_to: str = "", display: int = 10, page: int = 1) -> dict:
    """법제처(law.go.kr) 판례 검색 — 대법원·하급심 판례를 키워드로 찾는다.

    국세청 시스템(nts_ruling_search)의 법원판례와 별개로, 법제처가 제공하는
    전체 법원 판례 DB를 검색한다. 세무 판례의 상고심 확정 여부 확인에도 사용.

    Args:
        keyword: 검색어 (예: "청산금 양도시기")
        court: "대법원" 또는 "하위법원" (빈값 = 전체)
        date_from: 선고일 시작 YYYYMMDD (선택)
        date_to: 선고일 종료 YYYYMMDD (선택)
        display: 결과 수 (기본 10)
        page: 페이지 번호
    """
    return _safe(_law.search_cases, keyword, court, date_from, date_to, display, page)


@mcp.tool()
def court_case_detail(case_serial: str, max_chars: int = 8000) -> dict:
    """법제처 판례 본문 조회 — court_case_search 결과의 '판례일련번호'로
    판시사항·판결요지·참조조문·판례내용 전문을 가져온다."""
    return _safe(_law.get_case, case_serial, max_chars)


@mcp.tool()
def law_interpretation_search(keyword: str, display: int = 10,
                              serial: str = "") -> object:
    """법제처 법령해석례 검색/조회 (세법뿐 아니라 모든 법령의 해석례).

    serial 없이 호출하면 키워드 검색(안건명·회신기관·회신일자 목록),
    serial(해석례일련번호)을 주면 질의요지·회답·이유 전문을 반환한다.

    안건명 검색이라 "1세대 1주택"처럼 일반적인 말은 주택법·건축 해석례가 섞인다.
    세법 해석례를 찾을 때는 세목명·법령명을 함께 넣는다 (예: "양도소득세 1세대 1주택",
    "소득세법 시행령 제154조").
    """
    if serial:
        return _safe(_law.get_interpretation, serial)
    return _safe(_law.search_interpretations, keyword, display)


@mcp.tool()
def law_history_search(law_name: str, law_id: str = "", current_only: bool = False) -> object:
    """법령 연혁 조회 — 제정부터 현재까지 모든 시행본 목록(시행일자·공포번호·MST).

    세법은 개정이 잦아 예규·판례가 인용한 '당시 조문'을 봐야 할 때가 많다.
    이 도구로 시행본 목록을 확인하고, 특정 시점 조문은 law_article_as_of를 쓴다.
    목록의 공포번호를 law_addenda_search(promul_no=…)에 넣으면 그 개정의
    부칙(시행일·적용례·경과조치) 전문을 볼 수 있다.

    Args:
        law_name: 법령명 (예: "부가가치세법")
        law_id: 법령ID로 본법만 필터 (예: 부가가치세법=001571). 같은 이름의
                시행령·시행규칙 혼입을 막으려면 지정 권장.
        current_only: True면 현행 법령 검색만 (법령ID·MST 확인용)
    """
    if current_only:
        return _safe(_law.search_laws, law_name)
    return _safe(_law.law_history, law_name, law_id)


@mcp.tool()
def law_article_as_of(law_name: str, as_of_date: str, article_no: str,
                      law_id: str = "", max_chars: int = 6000) -> dict:
    """특정 날짜에 시행 중이던 법령 조문 원문 — '그 시점의 법'을 가져온다.

    예규 회신일·판결 선고일 당시의 조문을 확인할 때 사용한다. 연혁 시행본 중
    as_of_date 이하 최대 시행일자 본을 자동 선택해 조문을 잘라 반환한다.
    응답에 그 조문의 개별 시행일(조문시행일자)이 포함되며, 개정규정의 적용례
    ("시행 이후 양도분부터 적용" 등)까지 필요하면 law_addenda_search를 쓴다.

    Args:
        law_name: 법령명 (예: "소득세법 시행령")
        as_of_date: 기준일 YYYYMMDD (예: 예규 회신일 "20080715")
        article_no: 조번호 — "162" 또는 가지조문 "104의3" 형식 (패딩 없음)
        law_id: 법령ID 필터 (권장 — 본법/시행령 혼입 방지)
        max_chars: 원문 최대 길이 (기본 6000). 응답에 "잘림" 항목이 있으면
            거기 안내된 길이 이상으로 지정해 다시 호출하면 전문을 받는다.
    """
    return _safe(_law.law_article_as_of, law_name, as_of_date, article_no, law_id, max_chars)


@mcp.tool()
def law_article_diff(law_name: str, article_no: str, date_from: str,
                     date_to: str = "", law_id: str = "", max_chars: int = 8000,
                     find_change: bool = True) -> dict:
    """조문 개정 diff(신구조문 대비) — 두 시점 사이에 조문이 어떻게 바뀌었는지 비교.

    date_from(A)과 date_to(B, 생략하면 오늘=현행) 각각의 시행본에서 같은 조문을
    가져와 문안을 줄 단위로 대비한다. "이 예규가 나온 뒤에 조문이 바뀌었나?",
    "지금 문구는 언제부터 시행됐나?" 같은 질문에 사용.

    변경이 있으면:
    - diff: unified diff 형식 (-줄 = A에만, +줄 = B에만. 〈개정 …〉 주석 포함)
    - 현행_문안_시작: B 문안이 처음 시행된 시행본 (A~B 사이 시행본을 이진탐색으로 특정)
    - 개정_부칙: 그 개정 부칙의 시행일 요약 + 이 조문이 언급된 적용례·경과조치 발췌
      ("시행 후 양도분부터 적용" 등 — 언제 거래분부터 새 조문이 적용되는지)

    주의: A~B 사이 개정이 여러 번이면 마지막 변경만 특정된다. 응답의 안내에 따라
    date_to를 좁혀 재호출하면 그 이전 변경도 찾을 수 있다. find_change=True는
    시행본 원문을 여러 번 내려받으므로 수 초 걸릴 수 있다 (10분 캐시로 재호출은 빠름).

    Args:
        law_name: 법령명 (예: "소득세법", "법인세법 시행령")
        article_no: 조번호 — "104" 또는 가지조문 "104의3" 형식
        date_from: 비교 기준 A (YYYYMMDD, 예: 예규 회신일)
        date_to: 비교 기준 B (YYYYMMDD, 생략 시 오늘 = 현행)
        law_id: 법령ID 필터 (본법/시행령 혼입 방지, 권장)
        max_chars: diff 최대 길이 (기본 8000)
        find_change: False면 변경 시점 특정·부칙 연결을 생략 (빠른 비교만)
    """
    return _safe(_law.law_article_diff, law_name, article_no, date_from,
                 date_to, law_id, max_chars, find_change)


@mcp.tool()
def law_addenda_search(law_name: str = "", mst: str = "", law_id: str = "",
                       as_of_date: str = "", promul_no: str = "",
                       article_no: str = "", recent: int = 10,
                       max_chars: int = 8000) -> dict:
    """법령 부칙(附則) 조회 — 개정규정이 '언제 시행되고 어떤 분부터 적용되는지' 확인.

    개정 세법의 시행일, 적용례("이 법 시행 이후 양도하는 분부터 적용"), 경과조치는
    본문 조문이 아니라 부칙에 있다. 이 도구가 그 부칙을 가져온다. 세 가지 사용법:

    1) 목록: law_name(또는 mst)만 주면 부칙 목록을 최신순으로 반환
       — 각 부칙의 공포일자·공포번호·시행일 요약. 조문별 시행일이 다르면 그 내역도 포함
    2) 특정 개정의 부칙 전문: promul_no(공포번호) 지정
       — law_history_search 결과의 공포번호를 넣으면 그 개정의 시행일·적용례·경과조치 전문
    3) 특정 조문의 적용시기: article_no 지정 (예: "96", "104의3")
       — 전체 부칙에서 그 조문이 언급된 적용례·경과조치 항만 최신순으로 발췌

    Args:
        law_name: 법령명 (예: "소득세법", "법인세법 시행령") — mst 미지정 시 필수
        mst: 법령일련번호를 직접 지정 (law_history_search·law_article_as_of 결과의 MST)
        law_id: 법령ID 필터 (같은 이름의 본법/시행령 혼입 방지)
        as_of_date: YYYYMMDD — 이 날짜 당시 시행본의 부칙을 조회 (미지정 시 현행본)
        promul_no: 공포번호 (예: "21221") — 그 개정 부칙의 전문 반환
        article_no: 조번호 (예: "96", "57의2") — 그 조문이 언급된 부칙 항만 발췌
        recent: 목록 모드에서 반환할 부칙 수 (기본 10, 최신순)
        max_chars: 본문 최대 길이 (기본 8000). 응답에 "잘림"이 있으면 늘려서 재조회
    """
    return _safe(_law.law_addenda, mst, law_name, law_id, as_of_date,
                 promul_no, article_no, recent, max_chars)


@mcp.tool()
def admin_rule_search(keyword: str, serial: str = "", display: int = 10,
                      article: str = "", max_chars: int = 10000,
                      start_char: int = 0) -> object:
    """행정규칙(훈령·예규·고시) 검색/조회 — 조사사무처리규정·국세청 고시·외국환거래규정.

    serial 없이 호출하면 키워드 검색(규칙명·종류·소관부처·발령일자 목록),
    serial(일련번호)을 주면 본문을 반환한다.
    예: "조사사무처리규정", "외국환거래규정", "국세청 고시"

    국세청 기본통칙·집행기준은 법제처 행정규칙 DB에 없다(검색 0건) — 국세법령정보시스템
    검색(nts_ruling_search)으로 찾는다.

    외국환거래규정(30만 자) 같은 대형 고시는 전문이 max_chars에 잘리므로
    article에 조번호를 지정해 해당 조문만 받는 것을 권장한다
    (예: 해외직접투자 신고 = article="9-5"). 응답의 "잘림" 안내를 따르면 된다.

    Args:
        keyword: 검색어 (serial 없이 호출 시)
        serial: 행정규칙 일련번호 — 지정하면 본문 조회
        display: 검색 결과 수 (기본 10)
        article: 조번호 — "9-5"(제9-5조), "23", "23의2" 형식. 해당 조문만 반환
        max_chars: 본문 최대 길이 (기본 10000)
        start_char: 본문 시작 오프셋 — 조번호를 모를 때 이어 읽기용
    """
    if serial:
        return _safe(_law.get_admin_rule, serial, max_chars, article, start_char)
    return _safe(_law.search_admin_rules, keyword, display)


@mcp.tool()
def treaty_search(keyword: str = "", serial: str = "", display: int = 10,
                  article: str = "", lang: str = "ko", max_chars: int = 12000,
                  start_char: int = 0, occurrence: int = 1) -> object:
    """조약 검색/조회 — 조세조약 원문·발효일 확인, 조문 단위 읽기.

    serial 없이 호출하면 키워드 검색(조약명·서명일·발효일 목록),
    serial(조약일련번호)을 주면 조약 본문을 반환한다.
    예: "홍콩 소득에 대한 조세", "대한민국과 미합중국 간의 조세"

    조세조약은 수만 자라 전문이 max_chars에 잘린다. article에 조번호를 주면 그 조만
    잘라 준다. **조번호는 조약마다 다르다** — OECD 모델을 따른 조약은 대개 배당 10조·
    이자 11조·사용료 12조지만, 한·미 조약(1976)은 해운 10조·특수관계인 11조·배당 12조·
    이자 13조·사용료 14조다. 그러므로 처음에는 article 없이 호출해 앞부분 목차(조 표제)를
    확인하거나, 응답의 조 표제가 원하는 소득 종류인지 반드시 확인한 뒤 인용할 것.
    lang="en"이면 영문 정본 — 국문 번역과 해석이 갈릴 때 대조용.

    주의: law.go.kr 조약 본문은 서명 당시 협약문이다. 이후 개정의정서는 별도 조약으로
    등록되므로 keyword로 '○○ 개정의정서'를 검색해 함께 확인할 것 (제한세율처럼 숫자가
    결론을 좌우하면 반드시). 같은 조번호가 의정서에도 나오면 occurrence로 고른다.

    Args:
        keyword: 검색어 (serial 없이 호출 시)
        serial: 조약일련번호 — 지정하면 본문 조회
        display: 검색 결과 수 (기본 10)
        article: 조번호 — "10", "10의2" 형식. 해당 조만 반환
        lang: "ko"(국문, 기본) | "en"(영문)
        max_chars: 본문 최대 길이 (기본 12000)
        start_char: 조번호 없이 이어 읽을 때의 시작 오프셋 (응답 "잘림" 안내 참고)
        occurrence: 같은 조번호가 여러 번 나올 때 몇 번째 것인지 (1부터)
    """
    if serial:
        return _safe(_law.get_treaty, serial, max_chars, article, lang, start_char, occurrence)
    if not keyword:
        return {"status": "INVALID_INPUT", "오류": "keyword 또는 serial 중 하나는 필요합니다"}
    return _safe(_law.search_treaties, keyword, display)


@mcp.tool()
def ordinance_search(keyword: str, region: str = "", serial: str = "", display: int = 20,
                     max_chars: int = 10000, start_char: int = 0) -> object:
    """자치법규(조례·규칙) 검색/조회 — 지방세 탄력세율·감면조례 확인용.

    serial 없이 호출하면 키워드 검색, region으로 지자체 필터(예: "부산", "용산구").
    region을 주면 검색 결과를 최대 500건까지 넘겨 보며 거르므로 display가 작아도 된다.
    serial(검색 결과의 '일련번호')을 주면 조문·부칙 본문을 반환한다.
    예: keyword="시세 감면", region="부산" / keyword="부산광역시 시세 감면 조례"

    Args:
        keyword: 검색어 (자치법규명 기준)
        region: 지자체명 일부 (응답의 '지자체'에 이 문자열이 든 것만)
        serial: 자치법규 일련번호 — 지정하면 본문 조회
        display: 검색 결과 수 (기본 20)
        max_chars: 본문 최대 길이 (기본 10000)
        start_char: 본문 시작 오프셋 — 응답의 "잘림" 안내를 따라 이어 읽기
    """
    if serial:
        return _safe(_law.get_ordinance, serial, max_chars, start_char)
    return _safe(_law.search_ordinances, keyword, region, display)


@mcp.tool()
def law_article_search(law_name: str, keyword: str, law_id: str = "",
                       as_of_date: str = "", mst: str = "", max_results: int = 20) -> dict:
    """한 법령 안에서 키워드가 들어간 조문 찾기 — 조번호를 모를 때.

    "소득세법에서 '조정대상지역'이 나오는 조문", "조특법 중 '창업중소기업' 조문"처럼
    조번호를 모르는 상태에서 관련 조문을 좁힐 때 쓴다. 공백으로 나눈 단어는 모두
    들어 있어야 일치(AND). 제목에 걸린 조문을 먼저 보여준다. 결과는 발췌뿐이므로
    전문은 law_article_as_of로 읽는다.

    Args:
        law_name: 법령명 (예: "소득세법", "조세특례제한법 시행령")
        keyword: 검색어 (예: "조정대상지역", "1세대 1주택")
        law_id: 법령ID 필터 (같은 이름 본법/하위법령 혼입 방지)
        as_of_date: YYYYMMDD — 그 날짜 당시 시행본에서 검색 (미지정 시 현행)
        mst: 법령일련번호 직접 지정 (지정하면 law_name·as_of_date 무시)
        max_results: 최대 결과 수 (기본 20)
    """
    return _safe(_law.law_article_search, law_name, keyword, law_id, as_of_date, mst, max_results)


@mcp.tool()
def law_upcoming_changes(law_name: str, law_id: str = "", max_versions: int = 5) -> dict:
    """시행 예정 개정 — 공포됐지만 아직 시행되지 않은 개정과 시행일별 대상 조문.

    "내년부터 바뀌는 소득세법 조문", "공포됐는데 아직 시행 안 된 개정 있나?"에 쓴다.
    시행일마다 그날 시행되는 개정규정을 부칙 제1조(시행일 단서)에서 뽑아 주고,
    본칙 시행일이면 그 개정에서 바뀐 조문 목록을 준다. 거래·신고 시점이 시행일
    전후로 갈리는 사안에서 '어느 법을 적용하나'를 판단할 때 함께 확인할 것.

    공포 전 단계(국회 계류 법안, 입법예고된 시행령안)는 이 자료에 없다.

    Args:
        law_name: 법령명 (예: "소득세법", "법인세법 시행령")
        law_id: 법령ID 필터 (권장 — 같은 이름 하위법령 혼입 방지. 예: 소득세법=001565)
        max_versions: 가까운 시행일부터 몇 건까지 볼지 (기본 5)

    Returns:
        시행예정 목록 — 시행일자·공포번호·부칙_시행일 요약과, 그날 시행되는
        개정규정(언급_조문) 또는 개정_조문. 개정 후 문안은 law_article_as_of(시행일자)로,
        현행과의 차이는 law_article_diff로 이어서 확인.
    """
    return _safe(_law.law_upcoming, law_name, law_id, max_versions)


@mcp.tool()
def law_annex_search(law_name: str = "", annex_no: str = "", keyword: str = "",
                     kind: str = "", law_id: str = "", as_of_date: str = "", mst: str = "",
                     max_chars: int = 8000, search_scope: str = "title") -> dict:
    """별표·서식 조회 — 세율표·기준금액·업종 분류표 등 별표 본문과 원본 HWP·PDF 링크.

    세 가지 사용법:
    1) 목록: law_name만 주면 그 법령(시행본)의 별표·서식 목록 (번호·제목·최종개정·HWP/PDF 링크).
       '최종개정'은 별표 본문 머리의 <개정 …> 표기 — 원문에 별표별 시행일 필드는 없으므로
       시행 시점은 그 개정의 부칙(law_addenda_search)으로 확인.
       keyword를 함께 주면 제목·본문에 그 단어가 든 별표만.
    2) 본문: annex_no 지정 ("1", "1의2") — 그 별표 텍스트 전문 + 원본 링크.
    3) 법령 횡단 검색: law_name·mst 없이 keyword만 — 모든 법령의 별표·서식에서 검색
       (search_scope: "title" 별표명 / "law" 법령명 / "body" 별표 본문).

    별표는 대개 시행령·시행규칙에 있다 (예: "소득세법 시행령", "부가가치세법 시행규칙").
    텍스트로 바꾼 표는 칸이 어긋날 수 있으므로, 세율·금액이 결론을 좌우하면 HWP·PDF
    원본과 대조할 것.

    Args:
        law_name: 법령명 — 목록·본문 조회 시 (mst로 대신 가능)
        annex_no: 별표 번호 ("1", "1의2", "별표 3") — 지정하면 본문 반환
        keyword: 목록 필터 또는 횡단 검색어
        kind: "별표" 또는 "서식"으로 좁힘 (별표 1과 별지 제1호서식이 겹칠 때)
        law_id: 법령ID 필터
        as_of_date: YYYYMMDD — 그 날짜 당시 시행본의 별표 (미지정 시 현행)
        mst: 법령일련번호 직접 지정 (횡단 검색 결과의 관련법령MST)
        max_chars: 본문 최대 길이 (기본 8000)
        search_scope: 횡단 검색 범위 — "title"(기본) | "law" | "body"
    """
    if not law_name and not mst:
        if not keyword:
            return {"status": "INVALID_INPUT",
                    "오류": "law_name(또는 mst)이나 keyword 중 하나는 필요합니다"}
        return _safe(_law.search_annex_files, keyword, search_scope, kind)
    return _safe(_law.law_annexes, law_name, annex_no, keyword, kind, law_id,
                 as_of_date, mst, max_chars)


if __name__ == "__main__":
    logger.info("nts-tax-mcp 확장판 기동 — 기존 8개 + 법제처 13개 도구")
    mcp.run(transport="streamable-http")
