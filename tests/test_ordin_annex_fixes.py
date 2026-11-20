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
