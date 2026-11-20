# -*- coding: utf-8 -*-
"""
test_ordin_annex_fixes.py — 2026-11-20 점검에서 나온 문제의 회귀 테스트 (오프라인)

XML·HTML 샘플은 같은 날 사용자 PC 브라우저로 받은 실응답의 구조를 그대로 줄인 것이다
(소득세법 시행령 MST 290841 <별표단위>, 부산광역시 시세 감면 조례 MST 2130441,
olta 조심2025지1784 상세 페이지).

실행: python tests/test_ordin_annex_fixes.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from test_new_tools import _patched_get, _fixture_get  # noqa: E402

import law_go_kr as L  # noqa: E402

# ---------------------------------------------------------------------------
# 별표 — 시행일 필드는 원문에 없고, 본문 머리의 '<개정 …>'이 유일한 날짜
_ANNEX_REAL = """<?xml version="1.0" encoding="UTF-8"?>
<법령 법령키="0039562026093036737"><기본정보><법령ID>003956</법령ID>
<법령명_한글><![CDATA[소득세법 시행령]]></법령명_한글></기본정보>
<별표><별표단위 별표키="000100E">
<별표번호>0001</별표번호>
<별표가지번호>00</별표가지번호>
<별표구분>별표</별표구분>
<별표제목><![CDATA[농가부업규모 축산의 범위(제9조제1항제1호 관련)]]></별표제목>
<별표서식파일링크>/LSW/flDownload.do?flSeq=169813469</별표서식파일링크>
<별표HWP파일명><![CDATA[x.hwp]]></별표HWP파일명>
<별표서식PDF파일링크>/LSW/flDownload.do?flSeq=169813471</별표서식PDF파일링크>
<별표PDF파일명><![CDATA[x.pdf]]></별표PDF파일명>
<별표서식이미지파일링크>/LSW/flDownload.do?flSeq=169813473</별표서식이미지파일링크>
<별표이미지파일명><![CDATA[x.gif]]></별표이미지파일명>
<별표내용><![CDATA[■ 소득세법 시행령 [별표 1] <개정 2026. 2. 27.>                           ]]>
<![CDATA[
]]>
<![CDATA[  농가부업규모 축산의 범위(제9조제1항제1호 관련)          ]]>
<![CDATA[
]]>
<![CDATA[│젖소  │50마리  │]]>
</별표내용>
</별표단위>
<별표단위 별표키="000102E">
<별표번호>0001</별표번호><별표가지번호>02</별표가지번호><별표구분>별표</별표구분>
<별표제목><![CDATA[주택임차자금의 대출기관]]></별표제목>
<별표내용><![CDATA[■ 소득세법 시행령 [별표 1의2] <신설 2020. 2. 11.>]]>
<![CDATA[
<strong>대출기관</strong> 목록]]></별표내용>
</별표단위></별표></법령>"""


def _annex_get(endpoint, **params):
    assert params.get("target") == "law" and params.get("MST") == "290841", params
    return _ANNEX_REAL


def test_annex_amend_note_kept_and_reported():
    with _patched_get(_annex_get) as c:
        lst = c.law_annexes(mst="290841")
        one = c.law_annexes(mst="290841", annex_no="1")
    rows = lst["목록"]
    assert [r["최종개정"] for r in rows] == ["개정 2026. 2. 27.", "신설 2020. 2. 11."], rows
    assert all("시행일자" not in r for r in rows), "원문에 없는 시행일자 칸을 내보내면 안 됨"
    body = one["별표"]["본문"]
    assert "<개정 2026. 2. 27.>" in body, body[:80]   # 한글 꺾쇠 표기는 지우지 않는다
    assert "50마리" in body


def test_annex_html_tags_still_removed():
    with _patched_get(_annex_get) as c:
        r = c.law_annexes(mst="290841", annex_no="1의2")
    body = r["별표"]["본문"]
    assert "<strong>" not in body and "대출기관 목록" in body, body


def test_strip_html_only_ascii_tags():
    assert L._strip_html("a<b>c</b> <개정 2020. 1. 1.> <br/>d") == "ac <개정 2020. 1. 1.> d"
    assert L._amend_note("[별표 3] <개정 2024. 2. 29., 2025. 2. 28.>") == "개정 2024. 2. 29., 2025. 2. 28."
    assert L._amend_note("표만 있음") == ""


# ---------------------------------------------------------------------------
# 자치법규 검색 — 지역 필터는 display건 안에서가 아니라 여러 페이지를 넘겨 건다
def _ordin_item(i, name, org):
    return (f"<law id=\"{i}\"><자치법규일련번호>{1000 + i}</자치법규일련번호>"
            f"<자치법규명><![CDATA[{name}]]></자치법규명><지자체기관명>{org}</지자체기관명>"
            f"<공포일자>20260101</공포일자><시행일자>20260101</시행일자>"
            f"<제개정구분명>일부개정</제개정구분명></law>")


def _ordin_search_get(calls, n_total=150, busan_at=130):
    def g(endpoint, **params):
        assert endpoint == "lawSearch.do" and params["target"] == "ordin"
        calls.append(params)
        disp, page = params["display"], params.get("page", 1)
        start = (page - 1) * disp
        items = []
        for i in range(start, min(start + disp, n_total)):
            org = "부산광역시" if i == busan_at else f"가나시{i}"
            items.append(_ordin_item(i, f"{org} 시세 감면 조례", org))
        return f"<OrdinSearch><totalCnt>{n_total}</totalCnt>{''.join(items)}</OrdinSearch>"
    return g


def test_ordinance_region_scans_beyond_display():
    calls = []
    with _patched_get(_ordin_search_get(calls)) as c:
        rows = c.search_ordinances("시세 감면", region="부산", display=3)
    assert [r["지자체"] for r in rows] == ["부산광역시"], rows
    assert rows[0]["일련번호"] == "1130"
    assert len(calls) == 2 and all(p["display"] == 100 for p in calls), calls


def test_ordinance_region_not_found_is_explained():
    calls = []
    with _patched_get(_ordin_search_get(calls, n_total=40, busan_at=-1)) as c:
        try:
            c.search_ordinances("시세 감면", region="부산", display=3)
            raise AssertionError("지역 일치 0건은 NotFound여야 함")
        except L.LawGoKrNotFound as e:
            assert "40건" in str(e) and "부산" in str(e), e
    assert len(calls) == 1


def test_ordinance_without_region_unchanged():
    calls = []
    with _patched_get(_ordin_search_get(calls, n_total=10)) as c:
        rows = c.search_ordinances("시세 감면", display=3)
    assert len(rows) == 3 and calls[0]["display"] == 3


# ---------------------------------------------------------------------------
# 자치법규 본문 — serial(자치법규일련번호)은 MST로 보낸다
_ORDIN_BODY = """<?xml version="1.0" encoding="UTF-8"?><LawService><자치법규기본정보><자치법규ID>2064493</자치법규ID><자치법규일련번호>2130441</자치법규일련번호><공포일자>20260520</공포일자><공포번호>7946</공포번호><자치법규명><![CDATA[부산광역시 시세 감면 조례]]></자치법규명><시행일자>20260520</시행일자><자치법규종류>C0001</자치법규종류><지자체기관명>부산광역시</지자체기관명><제개정정보>일부개정</제개정정보></자치법규기본정보><조문><조 조문번호='000100'><조문번호>000100</조문번호><조문여부>Y</조문여부><조제목><![CDATA[목적]]></조제목><조내용><![CDATA[제1조(목적) 이 조례는 시세의 감면에 관한 사항을 규정한다.\\n]]></조내용></조><조 조문번호='000602'><조문번호>000602</조문번호><조문여부>Y</조문여부><조제목><![CDATA[기회발전특구로의 이전 등에 대한 감면]]></조제목><조내용><![CDATA[제6조의2(기회발전특구로의 이전 등에 대한 감면) 경감률: 100분의 50<개정 2017. 5. 31>\\n]]></조내용></조></조문><부칙><부칙공포일자>20260520</부칙공포일자><부칙공포번호>7946</부칙공포번호><부칙내용><![CDATA[부칙  제1조(시행일) 이 조례는 공포한 날부터 시행한다.\\n]]></부칙내용></부칙></LawService>""".replace("\\n", "\n")


def test_ordinance_body_uses_mst_and_parses_articles():
    seen = {}

    def g(endpoint, **params):
        seen.update(params)
        return _ORDIN_BODY
    with _patched_get(g) as c:
        r = c.get_ordinance("2130441")
    assert seen.get("MST") == "2130441" and "ID" not in seen, seen
    assert r["자치법규명"] == "부산광역시 시세 감면 조례" and r["지자체"] == "부산광역시"
    assert r["일련번호"] == "2130441" and r["자치법규ID"] == "2064493"
    body = r["본문"]
    assert body.startswith("제1조(목적)"), body[:40]       # 메타데이터 값이 본문 앞에 붙지 않음
    assert "000100Y" not in body and "<개정 2017. 5. 31>" in body
    assert "[부칙]" in body and "공포한 날부터 시행" in body


def test_ordinance_body_rejects_other_document():
    other = _ORDIN_BODY.replace("<자치법규일련번호>2130441<", "<자치법규일련번호>1950515<")
    with _patched_get(lambda e, **p: other) as c:
        try:
            c.get_ordinance("2130441")
            raise AssertionError("다른 일련번호 응답은 PARSE_ERROR여야 함")
        except L.LawGoKrParseError as e:
            assert "1950515" in str(e)


def test_ordinance_body_window():
    with _patched_get(lambda e, **p: _ORDIN_BODY) as c:
        r = c.get_ordinance("2130441", max_chars=20)
        r2 = c.get_ordinance("2130441", max_chars=20, start_char=20)
    full = L.LawGoKrClient._ordinance_text(_ORDIN_BODY)
    assert len(r["본문"]) == 20 and "start_char=20" in r["잘림"]
    assert r["본문"] == full[:20] and r2["본문"] == full[20:40] and r["본문길이"] == len(full)


# ---------------------------------------------------------------------------
# 부칙 — 원문 기본정보의 시행일자는 '본칙' 시행일로 표시
def test_addenda_header_labels_base_effective_date():
    with _patched_get(_fixture_get) as c:
        r = c.law_addenda(mst="280405", promul_no="21221", max_chars=200)
    assert "시행본_시행일자" not in r
    assert r["본칙_시행일자"] == "20260101" and r["공포번호"] == "21221", r


# ---------------------------------------------------------------------------
# olta 상세 — span 조각마다 줄을 바꾸지 않는다
_OLTA_DETAIL = """<html><body>
<div class="right_btn"><p class="download"><a href="#">다운로드</a></p><p>프린트</p></div>
<div id="print_contents"><div class="exp_ifview_cont size02"><div class="view_tt">
<p class="data_num">
\t\t\t조심2025지1784(20260226) 취득세<span>취소</span>
\t\t\t</p><p>쟁점 제목</p></div>
<div class="data_cont bgmark"><p class="data_tt t_01">참조조문</p><div>
\t\t\t지방세법 제13조
\t\t\t</div>
<p class="data_tt t_02">이유</p><div><p class="1"><span>1. </span><span>처분개요</span></p>
<p class="1">&nbsp;</p>
<p class="1"><span>&nbsp;&nbsp;가</span><span>. </span><span>청구법인은 </span><span>2022.2.16. </span><span>「</span><span>지방세법</span><span>」 </span><span>제</span><span>13</span><span>조 제</span><span>2</span><span>항</span></p>
</div></div></div></div></body></html>"""


def test_olta_detail_text_joins_inline_spans():
    from bs4 import BeautifulSoup
    from olta_tax_ruling_search import OltaTaxLawClient as O
    soup = BeautifulSoup(_OLTA_DETAIL, "html.parser")
    text = O._block_text(soup.find("div", id="print_contents"))
    lines = text.split("\n")
    assert "가. 청구법인은 2022.2.16. 「지방세법」 제13조 제2항" in lines, lines
    assert "1. 처분개요" in lines and "지방세법 제13조" in lines
    assert "조심2025지1784(20260226) 취득세 취소" in lines, lines
    assert "다운로드" not in text and "" not in lines


# ---------------------------------------------------------------------------
# 시행예정 — 이후 법률이 부칙 시행일을 바꾼 경우 (최신 공포본 부칙에서 근거를 찾는다)
_OLD_17757 = """<?xml version="1.0" encoding="UTF-8"?><법령><기본정보><법령ID>001565</법령ID>
<공포일자>20201229</공포일자><공포번호>17757</공포번호><법령명_한글><![CDATA[소득세법]]></법령명_한글>
<시행일자>20210101</시행일자></기본정보>
<조문><조문단위 조문키="0037001"><조문번호>37</조문번호><조문여부>조문</조문여부>
<조문제목><![CDATA[기타소득의 필요경비 계산]]></조문제목><조문내용><![CDATA[제37조(기타소득의 필요경비 계산)]]></조문내용></조문단위>
<조문단위 조문키="0164004"><조문번호>164</조문번호><조문가지번호>4</조문가지번호><조문여부>조문</조문여부>
<조문제목><![CDATA[가상자산 거래내역 등의 제출]]></조문제목><조문내용><![CDATA[제164조의4(가상자산 거래내역 등의 제출)]]></조문내용></조문단위></조문>
<부칙><부칙단위><부칙공포일자>20201229</부칙공포일자><부칙공포번호>17757</부칙공포번호>
<부칙내용><![CDATA[부칙 〈제17757호,2020.12.29〉
제1조(시행일) 이 법은 2021년 1월 1일부터 시행한다. 다만, 다음 각 호의 개정규정은 각 호의 구분에 따른 날부터 시행한다.
  2. 제37조제1항제3호 및 제164조의4의 개정규정: 2022년 1월 1일
제2조(일반적 적용례) 생략]]></부칙내용></부칙단위></부칙></법령>"""


def _deferred_history():
    base = {"법령명": "소득세법", "법령ID": "001565", "제개정구분": "일부개정"}
    return [dict(base, MST="224801", 시행일자="20270101", 공포일자="20201229", 공포번호="17757"),
            dict(base, MST="237497", 시행일자="20270101", 공포일자="20211208", 공포번호="18578"),
            dict(base, MST="280405", 시행일자="20260701", 공포일자="20251223", 공포번호="21221")]


def test_law_upcoming_follows_later_amended_addendum():
    def g(endpoint, **params):
        if params.get("MST") == "224801":
            return _OLD_17757
        if params.get("MST") == "237497":   # 18578 시행예정본 — 원문 부칙 그대로
            return _OLD_17757.replace("17757", "18578").replace("2022년 1월 1일", "2025년 1월 1일")
        return _fixture_get(endpoint, **params)
    with _patched_get(g) as c:
        c.law_history = lambda *a, **k: _deferred_history()
        r = c.law_upcoming("소득세법", law_id="001565", max_versions=5, today="20261120")
    v = next(v for v in r["시행예정"] if v["공포번호"] == "17757")
    assert "현재 부칙 문안" in v["근거"], v
    labels = [a["조문"] for a in v["언급_조문"]]
    assert "제37조" in labels and "제164조의4" in labels, labels
    refs = v.get("시행일_변경_근거") or []
    assert any(x["공포번호"] == "20615" and "2027년 1월 1일" in x["문장"] for x in refs), refs
    v2 = next(v for v in r["시행예정"] if v["공포번호"] == "18578")
    assert v2["근거"] == "확인 불가" and "연혁" in v2["안내"], v2


def test_later_addenda_refs_extracts_target_part():
    from test_parsers import read_gz
    units = L.LawGoKrClient._parse_addenda_units(read_gz("law_280405.xml.gz"))
    refs = L.LawGoKrClient._later_addenda_refs(units, "17757", "20270101", "20201229")
    hit = next(x for x in refs if x["공포번호"] == "20615")
    assert hit["대상"].strip().startswith("소득세법 일부개정법률 제37조제1항제3호"), hit
    assert not L.LawGoKrClient._later_addenda_refs(units, "18578", "20270101", "20211208")


# ---------------------------------------------------------------------------
# 시행예정 — 다른 법률의 부칙이 '아직 시행 전인 개정법'을 고친 경우 (제19563호 실측)
def _version_xml(promul_no, promul_date, arts):
    """arts: [(조번호, 가지번호, 제목, 변경Y여부)] — 시행예정본 원문의 조문 부분만 흉내 낸다."""
    units = "".join(
        f"<조문단위><조문번호>{no}</조문번호>"
        + (f"<조문가지번호>{br}</조문가지번호>" if br else "")
        + f"<조문여부>조문</조문여부><조문제목><![CDATA[{t}]]></조문제목>"
        f"<조문변경여부>{'Y' if ch else 'N'}</조문변경여부><조문내용><![CDATA[제{no}조]]></조문내용></조문단위>"
        for no, br, t, ch in arts)
    return (f"<?xml version=\"1.0\"?><법령><기본정보><공포일자>{promul_date}</공포일자>"
            f"<공포번호>{promul_no}</공포번호><법령명_한글><![CDATA[소득세법]]></법령명_한글>"
            f"<시행일자>20270101</시행일자></기본정보><조문>{units}</조문><부칙></부칙></법령>")


def _run_upcoming(rows, versions):
    def g(endpoint, **params):
        if params.get("MST") in versions:
            return versions[params["MST"]]
        return _fixture_get(endpoint, **params)
    base = {"법령명": "소득세법", "법령ID": "001565", "시행일자": "20270101"}
    hist = [dict(base, **r) for r in rows] + [dict(base, MST="280405", 시행일자="20260701",
                                                  공포일자="20251223", 공포번호="21221", 제개정구분="일부개정")]
    with _patched_get(g) as c:
        c.law_history = lambda *a, **k: hist
        return c.law_upcoming("소득세법", law_id="001565", max_versions=5, today="20261120")


def test_law_upcoming_follows_other_law_addendum_amending_pending_law():
    r = _run_upcoming(
        [dict(MST="252783", 공포일자="20230718", 공포번호="19563", 제개정구분="타법개정")],
        {"252783": _version_xml("19563", "20230718", [
            ("21", "", "기타소득", True), ("119", "", "비거주자의 국내원천소득", True)])})
    v = next(v for v in r["시행예정"] if v["공포번호"] == "19563")
    assert "법률 제17757호를 고침" in v["근거"], v
    assert [a["조문"] for a in v["언급_조문"]] == ["제21조", "제119조"], v["언급_조문"]
    assert any("제21조제1항제27호" in t for t in v["이날_시행되는_개정규정"]), v
    assert any(x["부칙"] == "17757" for x in v["연결_근거"]), v["연결_근거"]
    assert "연결_추정" not in v


def test_law_upcoming_guess_only_links_to_earlier_pending_law():
    """제18578호처럼 근거 문장이 없으면, 먼저 공포된 개정법의 그날 시행 항목과 겹치는
    '개정 표시 조문'만 추정으로 보여준다 — 나중에 공포된 법(제21221호)의 항목과는 잇지 않는다."""
    r = _run_upcoming(
        [dict(MST="237497", 공포일자="20211208", 공포번호="18578", 제개정구분="일부개정")],
        {"237497": _version_xml("18578", "20211208", [
            ("37", "", "기타소득의 필요경비 계산", True), ("118", "17", "재전입 등에 따른 환급 등", True),
            ("129", "", "원천징수세율", True)])})
    v = next(v for v in r["시행예정"] if v["공포번호"] == "18578")
    assert v["근거"].startswith("추정"), v
    assert [a["조문"] for a in v["언급_조문"]] == ["제37조"], v["언급_조문"]
    owners = {x["소속_공포번호"] for x in v["연결_추정"]}
    assert owners == {"17757"}, v["연결_추정"]


def test_amended_law_links_parses_other_law_part():
    from test_parsers import read_gz
    units = L.LawGoKrClient._parse_addenda_units(read_gz("law_280405.xml.gz"))
    u = next(u for u in units if u["공포번호"] == "19563")
    links = L.LawGoKrClient._amended_law_links(u["본문"], "소득세법")
    assert links and links[0]["개정법"] == "17757" and links[0]["조문"] == ["제21조", "제119조"], links
    assert not L.LawGoKrClient._amended_law_links(u["본문"], "법인세법")


# ---------------------------------------------------------------------------
# 시행예정 — 시행령(대통령령) 표기. 부칙 문안은 소득세법 시행령 현행본(MST 290841) 실응답에서 줄임
_SIHAENG_UNITS = [
    ("20220308", "32516", "부칙 <제32516호,2022.3.8>\n이 영은 2027년 1월 1일부터 시행한다. <개정 2022.12.31, 2024.12.31>"),
    ("20221231", "33207", "부칙 <제33207호,2022.12.31>\n제1조(시행일) 이 영은 2023년 1월 1일부터 시행한다. 다만, "
                          "대통령령 제32420호 소득세법 시행령 일부개정령 제150조의14제1항의 개정규정은 2025년 1월 1일부터 시행한다.\n"
                          "제2조(경과조치) 생략"),
    ("20241231", "35121", "부칙 <제35121호,2024.12.31>\n제1조(시행일) 이 영은 2025년 1월 1일부터 시행한다.\n제2조(경과조치) 생략"),
    ("20250228", "35349", "부칙 <제35349호,2025.2.28>\n제1조(시행일) 이 영은 공포한 날부터 시행한다. 다만, 다음 각 호의 "
                          "개정규정은 해당 호에서 정하는 날부터 시행한다. <개정 2025.11.28>\n"
                          "  1. 제4조제3항 및 별표 3의3의 개정규정: 2026년 1월 1일\n"
                          "  3. 대통령령 제32516호 소득세법 시행령 일부개정령 제183조제5항ㆍ제6항, 제207조제11항 및 제216조의4의 개정규정: 2027년 1월 1일\n"
                          "  4. 대통령령 제32516호 소득세법 시행령 일부개정령(대통령령 제33207호 소득세법 시행령 일부개정령 및 대통령령 "
                          "제35121호 소득세법 시행령 일부개정령에 따라 각각 개정된 내용을 포함한다) 제88조의 개정규정: 2027년 1월 1일\n"
                          "  6. 별표 5의 개정규정: 2028년 1월 1일\n"
                          "  7. 제187조의3제2항 및 제203조의 개정규정: 2026년 7월 1일\n제2조(적용례) 생략"),
    ("20251230", "35947", "부칙(재정경제부 직제) <제35947호,2025.12.30>\n제1조(시행일) 이 영은 2026년 1월 2일부터 시행한다. 다만, "
                          "부칙 제6조에 따라 개정되는 대통령령 중 이 영 시행 전에 공포되었으나 시행일이 도래하지 않은 대통령령을 "
                          "개정한 부분은 각각 해당 대통령령의 시행일부터 시행한다.\n제2조부터 제5조까지 생략\n"
                          "제6조(다른 법령의 개정) ①부터 <43>까지 생략\n  <44> 소득세법 시행령 일부를 다음과 같이 개정한다.\n"
                          "  제4조제2항ㆍ제4항, 제207조제1항 중 \"기획재정부령\"을 \"재정경제부령\"으로 한다.\n"
                          "  대통령령 제35349호 소득세법 시행령 일부개정령(대통령령 제35878호 소득세법 시행령 일부개정령에 따라 개정된 "
                          "내용을 포함한다) 제203조제2항 계산식 외의 부분 후단 중 \"기획재정부령\"을 \"재정경제부령\"으로 한다.\n"
                          "  대통령령 제32516호 소득세법 시행령 일부개정령(대통령령 제35349호 소득세법 시행령 일부개정령에 따라 개정된 "
                          "내용을 포함한다) 제216조의4 중 \"기획재정부령\"을 \"재정경제부령\"으로 한다.\n  <45>부터 <313>까지 생략"),
]


def _sihaeng_xml(arts, units=()):
    body = "".join(
        f"<조문단위><조문번호>{no}</조문번호>" + (f"<조문가지번호>{br}</조문가지번호>" if br else "")
        + f"<조문여부>조문</조문여부><조문제목><![CDATA[{t}]]></조문제목><조문변경여부>{'Y' if ch else 'N'}</조문변경여부>"
        f"<조문내용><![CDATA[제{no}조]]></조문내용></조문단위>" for no, br, t, ch in arts)
    add = "".join(f"<부칙단위><부칙공포일자>{d}</부칙공포일자><부칙공포번호>{n}</부칙공포번호>"
                  f"<부칙내용><![CDATA[{txt}]]></부칙내용></부칙단위>" for d, n, txt in units)
    return (f"<?xml version=\"1.0\"?><법령><기본정보><법령명_한글><![CDATA[소득세법 시행령]]></법령명_한글>"
            f"<시행일자>20261001</시행일자></기본정보><조문>{body}</조문><부칙>{add}</부칙></법령>")


_SIHAENG_ARTS = [("88", "", "가상자산에 대한 기타소득금액의 계산 등", True), ("183", "", "비거주자의 분리과세", True),
                 ("207", "", "비거주자에 대한 원천징수세액의 납부", True), ("216", "4", "가상자산 거래내역 등의 제출", True)]


def test_law_upcoming_sihaengryeong_notation():
    base = {"법령명": "소득세법 시행령", "법령ID": "003956", "시행일자": "20270101", "제개정구분": "일부개정"}
    rows = [dict(base, MST="241175", 공포일자="20220308", 공포번호="32516"),
            dict(base, MST="247489", 공포일자="20221231", 공포번호="33207"),
            dict(base, MST="267821", 공포일자="20241231", 공포번호="35121"),
            dict(base, MST="280865", 공포일자="20251230", 공포번호="35947", 제개정구분="타법개정"),
            dict(base, MST="269541", 공포일자="20250228", 공포번호="35349", 시행일자="20280101"),
            dict(base, MST="290841", 공포일자="20260930", 공포번호="36737", 시행일자="20261001")]
    old_units = {  # 시행예정본에 실린 공포 당시 부칙 (32516은 원래 2025년 시행)
        "241175": [("20220308", "32516", "부칙 <제32516호,2022.3.8>\n이 영은 2025년 1월 1일부터 시행한다.")],
        "247489": [_SIHAENG_UNITS[1]], "267821": [_SIHAENG_UNITS[2]], "280865": [_SIHAENG_UNITS[4]],
        "269541": [_SIHAENG_UNITS[3]]}
    arts_35947 = [("4", "", "거주자의 판정", True), ("207", "", "비거주자에 대한 원천징수세액의 납부", True)]

    def g(endpoint, **params):
        mst = params.get("MST")
        if mst == "290841":
            return _sihaeng_xml(_SIHAENG_ARTS, _SIHAENG_UNITS)
        if mst in old_units:
            arts = (arts_35947 if mst == "280865" else
                    [(a, b, t, False) for a, b, t, _ in _SIHAENG_ARTS] if mst == "241175" else _SIHAENG_ARTS)
            return _sihaeng_xml(arts, old_units[mst])
        raise AssertionError(params)
    with _patched_get(g) as c:
        c.law_history = lambda *a, **k: rows
        r = c.law_upcoming("소득세법 시행령", law_id="003956", max_versions=10, today="20261120")
    by = {(v["공포번호"], v["시행일자"]): v for v in r["시행예정"]}

    v = by[("32516", "20270101")]       # 현재 부칙의 본칙 시행일이 2027.1.1로 바뀜
    assert v["근거"].startswith("본칙 시행일") and "현재 부칙 문안" in v["근거"], v
    assert {x["공포번호"] for x in v.get("시행일_변경_근거", [])} == {"35349"}, v
    assert v["개정_조문"] == [] and [a["조문"] for a in v["언급_조문"]] == [
        "제88조", "제183조", "제207조", "제216조의4"], v

    for no in ("33207", "35121"):       # 제35349호 부칙 4호 괄호 안 '…에 따라 개정된 내용을 포함한다'
        v = by[(no, "20270101")]
        assert v["근거"].startswith("후속 법령 부칙"), (no, v)
        assert [a["조문"] for a in v["언급_조문"]] == ["제88조"], (no, v["언급_조문"])

    v = by[("35947", "20270101")]       # 한 줄형 '대통령령 제32516호 … 제216조의4 중 …'
    assert "대통령령 제32516호를 고침" in v["근거"], v
    assert [a["조문"] for a in v["언급_조문"]] == ["제216조의4"], v["언급_조문"]
    assert "연결_추정" not in v

    v = by[("35349", "20280101")]       # 별표만 있는 항목도 빈 목록이 아니어야 함
    assert [a["조문"] for a in v["언급_조문"]] == ["별표 5"], v["언급_조문"]


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
