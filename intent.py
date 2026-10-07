# -*- coding: utf-8 -*-
"""
질문 의도(Intent) 분류 — 규칙 기반 (v1.31)

왜 필요한가
    v1.30 까지는 모든 질문이 같은 경로(전체 검색 → 되묻기 → 선별 → 답변)를 탔습니다.
    "폐기물관리법 제25조 알려줘" 처럼 바로 답할 수 있는 질문에도 되묻기가 나갈 수
    있었고, "허가 기준이 뭐야?" 같은 일반 설명 질문에도 사용자 사실을 캐물었습니다.

    의도에 따라 경로를 나눕니다.
      ARTICLE_LOOKUP  법령명 + 제N조         → 그 조문만 조회해 답변 (되묻기·선별 없음)
      DEFINITION      "~이 뭐야", "뜻"       → 되묻기 없음
      GENERAL_RULE    기준·요건·절차·서류     → 되묻기 없음 (조건별로 나눠 설명)
      APPLICABILITY   우리/저희, ~해야 하나   → 되묻기 허용 (사용자 사실이 결론을 가름)
      DEADLINE        언제까지, 주기, 기한    → 되묻기 허용 (기준일이 필요)
      SANCTION        과태료, 벌칙, 위반하면  → 되묻기 허용
      OTHER           위에 안 걸림            → 되묻기 허용 (v1.30 과 같은 동작)

왜 LLM 이 아니라 규칙인가
    대부분 표면 신호(조문번호, 1인칭, 의문 어미)로 갈립니다. 로컬 모델에 맡기면
    같은 질문에도 판단이 흔들립니다. 애매하면 OTHER 로 두어 예전 동작을 유지합니다
    (= 되묻기를 막지 않는 쪽이 안전).

표준 라이브러리만 씁니다. 네트워크·LLM 을 부르지 않습니다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

ARTICLE_LOOKUP = "ARTICLE_LOOKUP"
DEFINITION = "DEFINITION"
GENERAL_RULE = "GENERAL_RULE"
APPLICABILITY = "APPLICABILITY"
DEADLINE = "DEADLINE"
SANCTION = "SANCTION"
OTHER = "OTHER"

LABEL = {
    ARTICLE_LOOKUP: "조문 조회",
    DEFINITION: "용어 정의",
    GENERAL_RULE: "일반 기준·절차 설명",
    APPLICABILITY: "적용 여부 판단",
    DEADLINE: "기한·주기",
    SANCTION: "제재·처분",
    OTHER: "일반 질문",
}

# 사용자 사실(용량·날짜·시설 등)이 결론을 가르는 의도 — 되묻기를 허용합니다.
NEEDS_FACTS = {APPLICABILITY, DEADLINE, SANCTION, OTHER}


@dataclass
class Intent:
    kind: str
    label: str
    needs_facts: bool
    signals: list = field(default_factory=list)   # 어떤 규칙이 걸렸는지 (로그·화면용)
    law: str = ""                                 # ARTICLE_LOOKUP: 법령명 후보 (원문 그대로)
    law_candidates: list = field(default_factory=list)
    jo: str = ""                                  # ARTICLE_LOOKUP: 조번호 "25"
    gaji: str = ""                                # ARTICLE_LOOKUP: 가지번호 "2" (제25조의2)

    def as_dict(self) -> dict:
        d = {"kind": self.kind, "label": self.label, "needs_facts": self.needs_facts,
             "signals": self.signals}
        if self.kind == ARTICLE_LOOKUP:
            d.update(law=self.law, jo=self.jo, gaji=self.gaji)
        return d


# ── 규칙 ─────────────────────────────────────────────────────
# 조문 번호: "제25조", "제 25 조의 2", "25조의2"(제 생략)
_ARTICLE_NO = re.compile(r"제?\s*(\d{1,4})\s*조(?:\s*의\s*(\d{1,3}))?(?![가-힣]*\s*관련)")

# 법령명처럼 끝나는 말 (조문번호 바로 앞에서 찾음)
_LAW_TAIL = re.compile(r"(법률|법|시행령|시행규칙|규칙|규정|령)$")

# 1인칭·자기 사례
_FIRST_PERSON = re.compile(
    r"(우리|저희|제가|내가|저는|나는|당사|본사|우리\s*회사|우리\s*공장|우리\s*사업장|우리\s*가게|"
    r"제\s*(가게|회사|공장|사업장|집|건물|땅|토지))")

# 적용 여부를 묻는 꼴
_APPLICABILITY = re.compile(
    r"(해당(되|하)(나요|는지|합니까|됩니까|나|니|는\s*건가요|는\s*거야)|"
    r"대상(인가요|인지|입니까|이야|이에요|인가|일까요|이\s*되나요)|"
    r"(받아야|해야|하여야|갖춰야|설치해야|신고해야|등록해야|허가받아야)\s*(하나|하나요|합니까|하는지|해요|돼요|되나요|할까요|하는\s*건가요)|"
    r"(초과|위반|미달)(한|하는|된|인)\s*(건가요|것인가요|건지|가요|건가|것입니까)|"
    r"(초과|위반)(인가요|인지|입니까|일까요)|"
    r"(가능한가요|가능합니까|가능한지|(해도|하면|안\s*해도|받으면)\s*되나요|"
    r"(할|될|받을|지정할|설치할|쓸|사용할)\s*수\s*(있나요|있습니까|있는지|있을까요|없나요)))")

# 사례를 구체적으로 적은 질문 — 수량·단위·날짜가 있으면 "일반론" 이 아니라 그 사례의 판단을
# 원하는 경우가 대부분입니다 (예: "68dB(A)였습니다", "15,000리터", "2010년 3월 15일").
_CASE_FACTS = re.compile(
    r"(\d[\d,\.]*\s*(리터|ℓ|L|톤|kg|킬로그램|㎡|제곱미터|m2|㎥|세제곱미터|dB|데시벨|회|번|명|대|기|"
    r"시간|개월|년|평|마리|두)|\d{4}\s*[년.\-/]\s*\d{1,2}\s*[월.\-/]\s*\d{1,2})")

_DEADLINE = re.compile(
    r"(언제까지|기한|유효기간|주기|몇\s*년\s*마다|몇\s*개월\s*마다|몇\s*년에\s*한\s*번|"
    r"다음\s*\S{0,10}\s*(검사|점검|신고|갱신|교육|측정)|언제\s*(받아야|해야|신고))")

_SANCTION = re.compile(
    r"(과태료|과징금|벌금|벌칙|처벌|행정처분|영업정지|조업정지|허가\s*취소|등록\s*취소|"
    r"위반하면|위반\s*시|안\s*하면|하지\s*않으면|어기면)")

_GENERAL_WORDS = re.compile(r"(기준|요건|절차|서류|방법|조건|요령|범위|종류|의무|대상|구비|제출)")
_ASK_WORDS = re.compile(r"(뭐|무엇|어떻게|어떤|알려|설명|정리|궁금|나요|인가요|입니까|\?)")

_DEFINITION = re.compile(
    r"(뜻|정의|의미|개념|이란|란\s*무엇|"
    r"(이|가|은|는)?\s*(뭐야|뭐예요|뭔가요|뭐지|무엇인가요|무엇입니까|뭐에요|뭘\s*말하나요))\s*\??\s*$")

# "법령명" 앞에 붙는 군더더기
_FILLER = {"그럼", "그러면", "혹시", "근데", "그런데", "그리고", "또", "또한", "다시", "참고로",
           "질문", "궁금한데", "궁금합니다", "알려줘", "알려주세요"}


def _law_candidates_before(text: str) -> list[str]:
    """
    조문번호 앞쪽 글에서 법령명 후보를 만듭니다. 긴 것부터.
      "그럼 폐기물관리법 시행규칙" → ["그럼 폐기물관리법 시행규칙", "폐기물관리법 시행규칙"]
    「」 가 있으면 그 안의 이름 하나만 씁니다.
    """
    m = re.findall(r"「([^」]{2,60})」", text)
    if m:
        return [m[-1].strip()]
    tail = re.sub(r"[\s,.:;()\[\]\"'“”‘’]+$", "", text)
    tail = re.sub(r"\s*의$", "", tail)
    words = [w for w in re.split(r"\s+", tail) if w]
    if not words or not _LAW_TAIL.search(words[-1]):
        return []
    # 마지막 단어가 "시행령/시행규칙" 뿐이면 앞 단어와 붙여야 이름이 됩니다.
    out = []
    for i in range(max(0, len(words) - 6), len(words)):
        cand_words = words[i:]
        if cand_words[0] in _FILLER:
            continue
        cand = " ".join(cand_words)
        if cand in ("시행령", "시행규칙", "법", "법률", "규칙", "령"):
            continue
        if len(cand.replace(" ", "")) < 3:
            continue
        out.append(cand)
    return out


def classify(question: str) -> Intent:
    q = re.sub(r"\s+", " ", str(question or "")).strip()
    signals = []

    # 1) 조문 직접 조회 — 법령명 + 제N조 가 있고, 사례 판단을 묻지 않을 때
    for m in _ARTICLE_NO.finditer(q):
        before = q[:m.start()]
        cands = _law_candidates_before(before)
        if not cands:
            continue
        if _FIRST_PERSON.search(q) or _APPLICABILITY.search(q) or _DEADLINE.search(q):
            signals.append("조문번호 있음(사례 판단 질문이라 조회로 보지 않음)")
            break
        return Intent(ARTICLE_LOOKUP, LABEL[ARTICLE_LOOKUP], False,
                      signals=[f"조문번호: {m.group(0).strip()}"],
                      law=cands[-1] if len(cands) == 1 else cands[0],
                      law_candidates=cands, jo=m.group(1), gaji=m.group(2) or "")

    fp = _FIRST_PERSON.search(q)
    ap = _APPLICABILITY.search(q)
    dl = _DEADLINE.search(q)
    sc = _SANCTION.search(q)

    # 2) 사용자 사례 판단 — 1인칭이거나 적용 여부를 묻는 꼴
    if ap or (fp and (_GENERAL_WORDS.search(q) or _ASK_WORDS.search(q))):
        if fp:
            signals.append(f"1인칭: {fp.group(0)}")
        if ap:
            signals.append(f"적용 여부: {ap.group(0)}")
        kind = DEADLINE if dl else APPLICABILITY
        if dl:
            signals.append(f"기한: {dl.group(0)}")
        return Intent(kind, LABEL[kind], True, signals=signals)

    # 2-1) 수량·날짜 같은 사례 사실을 적은 질문 — 그 사례에 대한 판단을 원함
    cf = _CASE_FACTS.search(q)
    if cf and not dl and not sc:
        return Intent(APPLICABILITY, LABEL[APPLICABILITY], True,
                      signals=[f"사례 사실: {cf.group(0).strip()}"])

    # 3) 기한·주기
    if dl:
        return Intent(DEADLINE, LABEL[DEADLINE], True, signals=[f"기한: {dl.group(0)}"])

    # 4) 제재
    if sc:
        return Intent(SANCTION, LABEL[SANCTION], True, signals=[f"제재: {sc.group(0)}"])

    # 5) 일반 기준·절차 (사례가 아니라 기준 자체를 물음)
    gw = _GENERAL_WORDS.search(q)
    if gw and (_ASK_WORDS.search(q) or re.search(r"(기준|요건|절차|서류|방법|조건|요령|범위|종류|의무)\s*$", q)):
        return Intent(GENERAL_RULE, LABEL[GENERAL_RULE], False,
                      signals=[f"일반 기준어: {gw.group(0)}"])

    # 6) 용어 정의
    df = _DEFINITION.search(q)
    if df and len(q) <= 60:
        return Intent(DEFINITION, LABEL[DEFINITION], False,
                      signals=[f"정의 질문: {df.group(0).strip()}"])

    return Intent(OTHER, LABEL[OTHER], True, signals=signals or ["규칙 해당 없음"])
