# -*- coding: utf-8 -*-
"""패치 단위 검증. 네트워크 없이 순수 함수 + 파서만 확인합니다."""
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["LAW_OC"] = "dummy"
import law_client as L  # noqa: E402

ok = fail = 0


def eq(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print(f"  PASS  {label}")
    else:
        fail += 1
        print(f"  FAIL  {label}\n          got ={got!r}\n          want={want!r}")


print("\n[1] _clamp_display — 매뉴얼 max=100")
for v, w in [(20, 20), (100, 100), (1000, 100), (0, 1), (-5, 1),
             ("50", 50), (None, 20), ("abc", 20)]:
    eq(f"_clamp_display({v!r})", L._clamp_display(v), w)

print("\n[2] _jo_code — 매뉴얼: 조번호(4)+조가지번호(2)")
for args, w in [
    (("2",),          "000200"),   # 제2조
    (("10", "2"),     "001002"),   # 제10조의2
    ((72,),           "007200"),   # 제72조
    (("0072",),       "007200"),   # 0패딩 4자리 → 조번호
    (("007200",),     "007200"),   # 이미 6자리면 그대로
    (("제72조",),      "007200"),   # 숫자만 추출
    (("53", "0"),     "005300"),   # 가지번호 0 은 없는 것과 동일
    (("",),           ""),         # 빈 값
    ((None,),         ""),
]:
    eq(f"_jo_code{args}", L._jo_code(*args), w)

print("\n[3] _parse_search_rows — totalCnt 추출 + 연혁본 제외")
xml = """<LawSearch>
  <target>eflaw</target><totalCnt>137</totalCnt><page>1</page>
  <law id="1"><법령일련번호>281911</법령일련번호><현행연혁코드>현행</현행연혁코드>
    <법령명한글>토양환경보전법</법령명한글><법령ID>001234</법령ID></law>
  <law id="2"><법령일련번호>111111</법령일련번호><현행연혁코드>연혁</현행연혁코드>
    <법령명한글>토양환경보전법</법령명한글><법령ID>001234</법령ID></law>
  <law id="3"><법령일련번호>222222</법령일련번호>
    <법령명한글>토양환경보전법 시행령</법령명한글><법령ID>005678</법령ID></law>
</LawSearch>"""
rows, total = L._parse_search_rows(ET.fromstring(xml))
eq("totalCnt", total, 137)
eq("연혁본 제외 후 건수", len(rows), 2)
eq("현행연혁코드 없는 레코드는 통과", rows[1]["법령명한글"], "토양환경보전법 시행령")

print("\n[4] get_delegated 파서 — 위임 종류별 레코드 인정 (3번 핵심)")
# ★ 2026-08-18 — 아래 XML 은 **실측한 응답 구조**입니다.
#   (/api/raw?target=lsDelegated&value=000160&mode=detail — 토양환경보전법)
#   예전 픽스처는 매뉴얼 필드 목록만 보고 "평평한 <조문>" 으로 지어낸
#   것이었는데, 실제 응답은 위임조문정보 > (조정보 | 위임정보 > *조문정보)
#   3단 중첩이라 픽스처 자체가 틀려 있었습니다. 실측 모양으로 바꿉니다.
dele_xml = """<lsDelegated><법령>
  <법령정보><법령ID>001234</법령ID><법령명>토양환경보전법</법령명></법령정보>
  <위임조문정보>
    <조정보><조문번호>53</조문번호><조문제목>권한의 위임</조문제목></조정보>
    <위임정보>
      <위임구분>시행령</위임구분>
      <위임법령일련번호>281912</위임법령일련번호>
      <위임법령제목>토양환경보전법 시행령</위임법령제목>
      <위임법령조문정보>
        <위임법령조문번호>72</위임법령조문번호>
        <위임법령조문제목>권한의 위임</위임법령조문제목>
        <조항호목>제53조제1항제2호</조항호목>
      </위임법령조문정보>
      <위임법령조문정보>
        <위임법령조문번호>72</위임법령조문번호>
        <위임법령조문제목>권한의 위임</위임법령조문제목>
        <조항호목>제53조제1항제3호</조항호목>
      </위임법령조문정보>
    </위임정보>
    <위임정보>
      <위임구분>위임행정규칙</위임구분>
      <위임행정규칙조문정보>
        <위임행정규칙일련번호>2100000123</위임행정규칙일련번호>
        <위임행정규칙제목>토양오염물질 위해성평가 지침</위임행정규칙제목>
        <조항호목>제53조제2항</조항호목>
      </위임행정규칙조문정보>
    </위임정보>
    <위임정보>
      <위임구분>인용법령</위임구분>
      <위임법령일련번호>211557</위임법령일련번호>
      <위임법령제목>위험물안전관리법</위임법령제목>
      <위임법령조문정보>
        <위임법령조문번호>0</위임법령조문번호>
        <조항호목>제53조제4항</조항호목>
      </위임법령조문정보>
    </위임정보>
  </위임조문정보>
  <위임조문정보>
    <조정보>
      <조문번호>15</조문번호><조문가지번호>2</조문가지번호>
      <조문제목>토양오염검사</조문제목>
    </조정보>
    <위임정보>
      <위임구분>시행규칙</위임구분>
      <위임법령제목>토양환경보전법 시행규칙</위임법령제목>
      <위임법령조문정보>
        <위임법령조문번호>12</위임법령조문번호>
        <조항호목>제15조의2제1항</조항호목>
      </위임법령조문정보>
    </위임정보>
    <위임정보>
      <위임법령조문정보>
        <위임법령조문번호>4</위임법령조문번호>
        <위임법령조문가지번호>2</위임법령조문가지번호>
        <조항호목>제15조의2제3항</조항호목>
      </위임법령조문정보>
    </위임정보>
  </위임조문정보>
  <위임조문정보>
    <조정보><조문번호>99</조문번호><조문제목>연결 없는 조문</조문제목></조정보>
  </위임조문정보>
</법령></lsDelegated>"""

# _get 을 가로채 네트워크 없이 파서만 돌립니다.
L._get = lambda url, params: dele_xml
rows = L.get_delegated("001234")

# 제53조: 시행령 제72조(항·호 2건이 같은 조문 → 1건으로 합쳐짐) + 위임행정규칙
#         + 인용법령. 제15조의2: 시행규칙 제12조. (제99조 제외, 제목없는 위임정보 제외)
eq("레코드 수 (중복 합침·제목없음 제외·제99조 제외)", len(rows), 4)
kinds = [r["_kind"] for r in rows]
eq("위임행정규칙이 살아남았는지 (예전엔 버려짐)", "행정규칙" in kinds, True)
eq("정렬: 시행령→시행규칙→행정규칙→인용법령",
   [r["_kind_raw"] for r in rows], ["시행령", "시행규칙", "위임행정규칙", "인용법령"])
adm = [r for r in rows if r["_kind"] == "행정규칙"][0]
eq("_title (행정규칙)", adm["_title"], "토양오염물질 위해성평가 지침")
eq("_seq (행정규칙 일련번호)", adm["_seq"], "2100000123")
eq("_jo (행정규칙은 조문번호 없음)", adm["_jo"], "")
eq("_jo (시행령)", rows[0]["_jo"], "72")
eq("같은 대상 조문은 1건으로 합치고 출처를 모음",
   rows[0]["_from"], ["제53조제1항제2호", "제53조제1항제3호"])
ref = [r for r in rows if r["_kind_raw"] == "인용법령"][0]
eq("조문번호 0 은 '제0조' 가 아니라 빈 값", ref["_jo"], "")
eq("가지조문 조문가지번호 (조정보에서 직접)",
   [r for r in rows if r["조문번호"] == "15"][0]["조문가지번호"], "2")
eq("가지 없는 조문은 빈 문자열", rows[0]["조문가지번호"], "")
eq("제목 없는 <위임정보> 는 버림 (어느 법령인지 알 수 없음)",
   any(r["조문번호"] == "15" and r["_jo"] == "4" for r in rows), False)

print("\n[5] delegation_map — 0패딩/비패딩 양쪽 키")
m = L.delegation_map("001234")
eq("'53|' 로 찾기 (본문 조회 형식)", len(m.get(L.dele_key("53"), [])), 3)
eq("가지조문 '15|2' 분리", len(m.get(L.dele_key("15", "2"), [])), 1)
eq("가지조문이 '15|' 에 안 섞임", m.get(L.dele_key("15")), None)
eq("연결 없는 제99조는 키 자체가 없음", m.get(L.dele_key("99")), None)
import json as _json
eq("맵이 JSON 직렬화 가능 (튜플 키 금지)", isinstance(_json.dumps(m, ensure_ascii=False), str), True)

print("\n[6] get_article — MST+eflaw 는 efYd 필수 (매뉴얼)")
try:
    L.get_article("eflaw", mst="166520", jo_no="3")
    eq("efYd 없이 MST 조회 → 에러", False, True)
except L.LawApiError as e:
    eq("efYd 없이 MST 조회 → 에러", "efYd" in str(e), True)
try:
    L.get_article("eflaw", jo_no="")
    eq("JO 없이 호출 → 에러", False, True)
except L.LawApiError as e:
    eq("JO 없이 호출 → 에러", "JO" in str(e), True)

print("\n[7] HTML 전용 target 가드 (2번 관련 부수효과)")
try:
    L.search("lsHistory", "자동차관리법")
    eq("lsHistory 검색 → 명확한 에러", False, True)
except L.LawApiError as e:
    eq("lsHistory 검색 → 명확한 에러", "HTML" in str(e), True)

print("\n[8] get_article 응답 파싱 (JO 지정 조회)")
art_xml = """<법령>
  <기본정보><법령명_한글>토양환경보전법 시행령</법령명_한글><시행일자>20250101</시행일자></기본정보>
  <조문><조문단위>
    <조문번호>72</조문번호><조문제목>권한의 위임</조문제목>
    <조문내용>제72조(권한의 위임) 환경부장관은 다음 권한을 위임한다.</조문내용>
    <항><항내용>① 다음 각 호의 권한</항내용>
      <호><호내용>1. 토양오염도 검사</호내용></호>
      <목><목내용>가. 시료 채취</목내용></목>
    </항>
  </조문단위></조문>
</법령>"""
L._get = lambda url, params: art_xml
got = L.get_article("eflaw", law_id="005678", jo_no="72")
eq("meta 법령명", got["meta"].get("법령명_한글"), "토양환경보전법 시행령")
eq("조문 1건", len(got["articles"]), 1)
eq("조문번호", got["articles"][0]["조문번호"], "72")
body = got["articles"][0]["조문내용"]
eq("항 포함", "① 다음 각 호의 권한" in body, True)
eq("호 포함", "1. 토양오염도 검사" in body, True)
eq("목 포함 (항의 형제로 있는 목)", "가. 시료 채취" in body, True)

print(f"\n{'=' * 56}\n  PASS {ok} / FAIL {fail}\n{'=' * 56}")
sys.exit(1 if fail else 0)
