"""
LLM 호출부. 로컬 LLM 서버를 OpenAI 호환 API(/v1/chat/completions)로 부릅니다.

  현재 구성: llama.cpp 의 llama-server + Qwen3.6-35B-A3B (Q4_K_M)
    - start.bat 이 llama-server 를 같이 띄웁니다 (포트 8080, 컨텍스트 -c 16384).
    - .env
        LOCAL_BASE_URL=http://localhost:8080/v1
        LOCAL_MODEL=Qwen3.6-35B-A3B     (llama-server 는 모델 하나만 쓰므로 표시용)

  Ollama 도 OpenAI 호환 API 를 제공하므로 LOCAL_BASE_URL 을 11434 포트로 바꾸면
  그대로 쓸 수 있습니다 (LOCAL_MODEL 에 ollama 모델 이름을 넣으세요).

  ★ 2026-09-29 — 클라우드 LLM 경로를 모두 걷어냈습니다. 로컬 전용입니다.
    키 순회·한도(429) 처리·대체 모델·단계별 백엔드 선택 코드가 함께 빠졌습니다.
"""

import json
import os
import re
import time

import httpx

import applog

# ── 출력 토큰 상한 ────────────────────────────────────────────
# 상한이 없으면 모델이 계속 생성합니다.
# 단계마다 필요한 분량이 다르므로 따로 잡습니다.
# (llama-server 에서는 남은 컨텍스트보다 크면 자동으로 줄여서 보냅니다)
MAXTOK_TERMS   = int(os.getenv("MAXTOK_TERMS",   "300"))    # 두 줄
MAXTOK_SELECT  = int(os.getenv("MAXTOK_SELECT",  "400"))    # 번호 나열
MAXTOK_CLARIFY = int(os.getenv("MAXTOK_CLARIFY", "800"))    # 질문 몇 줄
MAXTOK_ANSWER  = int(os.getenv("MAXTOK_ANSWER",  "4000"))   # 근거 + 설명

# 조문 선별에서 받아들일 최대 개수. 프롬프트도 5~20개를 요구합니다.
# ★ 2026-09-29 — 예전에는 40개까지 받아서, 답변 프롬프트가 컨텍스트(16,384)를
#   넘는 원인 중 하나였습니다.
SELECT_MAX = int(os.getenv("SELECT_MAX", "20"))

# ── 로컬 LLM 서버 ─────────────────────────────────────────────
LOCAL_BASE_URL = os.getenv("LOCAL_BASE_URL", "http://localhost:8080/v1").rstrip("/")
LOCAL_MODEL = os.getenv("LOCAL_MODEL", "Qwen3.6-35B-A3B")
LOCAL_TIMEOUT = float(os.getenv("LOCAL_TIMEOUT", "300"))   # 로컬은 느리므로 넉넉히
# 서버에서 컨텍스트 길이를 읽지 못할 때 쓸 값. llama-server 의 -c 와 맞추세요.
LOCAL_CTX_FALLBACK = int(os.getenv("LOCAL_CTX", "16384"))

# 사고 과정(Thinking)을 끌지. 이 프로그램은 형식 준수가 중요하지
# 추론이 필요한 작업이 아니므로 끄는 편이 훨씬 빠릅니다.
# (프롬프트에 /no_think 를 붙이는 방식은 Qwen 계열에서 듣지 않았습니다)
#   · llama-server: /v1/chat/completions 에
#                   chat_template_kwargs={"enable_thinking": false} 를 보냅니다.
#   · Ollama      : 네이티브 /api/chat 에 think=false 를 보냅니다.
LOCAL_NO_THINK = os.getenv("LOCAL_NO_THINK", "1") not in ("0", "", "false", "False")

# 어느 서버인지. 비워두면 주소로 판별합니다 (포트 11434 → ollama, 그 외 → llamacpp).
# llama-server 에는 Ollama 전용 /api/chat 이 없어서 구분이 필요합니다.
_srv = os.getenv("LOCAL_SERVER", "").strip().lower()
LOCAL_SERVER = _srv if _srv in ("ollama", "llamacpp") else (
    "ollama" if ":11434" in LOCAL_BASE_URL else "llamacpp")

# 1 이면 프롬프트와 응답 전문을 _runs/llm_debug_YYYYMMDD.log 에 남깁니다.
# (콘솔에는 찍지 않습니다 — 콘솔이 읽을 수 없을 만큼 길어졌습니다)
LLM_DEBUG = os.getenv("LLM_DEBUG", "0") not in ("0", "", "false", "False")


class AiError(Exception):
    pass


def _dbg(msg: str) -> None:
    """파싱 과정 메모. LLM_DEBUG=1 일 때 디버그 파일에만 남깁니다."""
    applog.debug("parse", msg)


# --- 프롬프트 --------------------------------------------------------
# 이 프로그램의 핵심입니다. 여기가 부실하면 AI가 아는 척하며 지어냅니다.

TERM_PROMPT = """너는 대한민국 국가법령 검색을 돕는 도구다.
질문자는 행정 실무자이거나 일반 시민이다. 분야는 정해져 있지 않다.

사용자의 일상적인 질문을 법령에서 실제로 쓰이는 용어로 바꾸는 일만 한다.
설명·인사·사고 과정을 쓰지 마라.
{output_rules}

규칙:
- **용어가 가장 중요하다.** 법령 본문에 실제로 나올 법한 법률 용어(제도·시설·행위·의무 이름)를 쓴다.
  생활 표현을 그대로 쓰지 말고 법령 표현으로 바꾼다.
  예) "기름 새는지 검사" → 누출검사,  "가게 차리기 전 신고" → 영업신고
- 법령명은 **추측 후보**일 뿐이다(검색 결과로 다시 확인한다). 확신이 없으면 1개만 쓴다.
  정식 명칭으로 쓰고 "시행령", "시행규칙" 은 붙이지 않는다.
- 같은 용어가 여러 법에 쓰이면 **질문의 시설·행위에 가장 직접 적용되는 법**을 앞에 둔다.
- **질문이 일반적인 상황이면 일반법을 고르라.**
  질문에 명시되지 않은 특수·예외 분야 법을 1순위로 올리지 마라.
  예) "사업장에서 폐기물을 배출한다" → 폐기물관리법 (O)
      방사성폐기물 관리법 (X — 질문에 방사성이라는 말이 없다)
- 조문 번호(제○조)를 지어내지 마라.
- 영어를 쓰지 마라. 한국어 법령 용어만 쓴다.

예시 1)
질문: 주유소 땅이 기름으로 오염됐는지 조사하는 절차
법령명: 토양환경보전법
용어: 특정토양오염관리대상시설, 토양오염도검사, 토양정밀조사

예시 2)
질문: 음식점을 열려면 어디에 무슨 신고를 해야 하나
법령명: 식품위생법
용어: 영업신고, 식품접객업, 일반음식점영업

예시 3)
질문: 창고로 쓰던 건물을 사무실로 바꾸려면 허가가 필요한가
법령명: 건축법
용어: 용도변경, 건축물대장 기재내용 변경, 용도변경 허가

질문: {question}
"""

_TERM_RULES_TEXT = """아래 두 줄만 출력한다.

법령명: (실제 법령 이름 1~2개, 쉼표로 구분)
용어: (실제 법령용어 3~5개, 쉼표로 구분)
- 백틱(`), 따옴표, 괄호, 번호를 붙이지 마라."""

_TERM_RULES_JSON = """JSON 하나만 출력한다.
  {"laws": ["법령 이름 1~2개"], "terms": ["법령 용어 3~5개"]}"""

TERM_SCHEMA = {
    "type": "object",
    "properties": {
        "laws": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "terms": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 6},
    },
    "required": ["laws", "terms"],
}


ANSWER_PROMPT = """너는 대한민국 국가법령 조문을 근거로 답하는 조회 도우미다.
사용자는 행정 실무자이거나 일반 시민이며, 법률 전문가가 아니다. 쉽게 설명하되, 근거는 [조문 원문] 안에서만 찾는다.

━━ 최우선 원칙 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. [조문 원문]에 실제로 있는 내용만 답한다.
   법률 지식·판례·행정해석·기억·상식으로 보충하지 마라.
2. 원문에 없는 법령명·조문번호·항·호·요건·기간·금액을 만들어내지 마라.
3. **질문에서 특정되지 않은 조건은 임의로 특정하지 마라.**
   조건이 "모름" 이면 그 경우를 하나로 확정하지 말고, 경우를 나눠 설명하라.
4. 법률·시행령·시행규칙은 **서로 다른 법령**이다.
   같은 제○조라도 반드시 법령명을 함께 확인하라. 번호만 보고 인용하지 마라.
5. 필요한 하위 법령이 [조문 원문]에 없으면 추측하지 말고
   "제공된 조문에서 확인할 수 없다" 고 명시하라.
6. **[조문 원문]이 질문과 전혀 다른 분야이면** 그 사실을 첫 줄에 밝혀라.
   조문에 없는 답을 지어내지 말고, 어떤 법령이 필요한지 알려라.
   예) "질문은 사업장폐기물에 관한 것이나 제공된 조문은 방사성폐기물
        관련 규정입니다. 「폐기물관리법」 조문이 필요합니다."
7. ★ **[질문] 원문에 적힌 사실이 (확인된 조건)의 "모름" 보다 우선한다.**
   질문에 "오후 3시", "하루 2시간", "주거지역", "200kg" 처럼 적혀 있으면 그 값으로 판단한다.
   되묻기에 "모름" 이라고 답했더라도 질문 원문에 있는 사실이면 모르는 것이 아니다.
8. **경우를 나눌 때** 결과가 같은 경우는 나누지 말고 한 번에 쓴다.
   질문의 전제와 모순되는 경우는 만들지 마라.
   예) "1년 동안 80% 이상 출근" 이라고 했는데 "1년 미만이면 …" 으로 나누지 마라.
9. **최종 판단만 쓴다.** "수정:", "다시 확인:", "정정하면" 처럼 쓰다가 고친 흔적을 남기지 마라.
   【결론】의 판정과 【계산】·【설명】의 판정이 서로 달라서는 안 된다. 쓰기 전에 먼저 판정을 정하라.
   【결론】에 "가능성이 높다" 같은 추측을 쓰지 마라. 경우를 나눠도 모든 경우의 판정이 같으면
   그 판정 하나만 쓴다. 되묻기에 "모름" 이라 답한 조건이 판정을 바꾸지 않으면 언급하지 마라.
10. 한국어로만 쓴다. 일본어(への·の 등)·중국어 글자를 섞지 마라.
11. **질문이 여러 가지를 물으면 하나도 빠뜨리지 말고 각각 답한다.**
    예) "초과한 건가요? 초과라면 어떤 조치를 받나요?" → 초과가 아니라고 판정했더라도
        "초과했다면 받게 되는 조치" 를 조문으로 한두 줄 덧붙인다.
12. ★ **(확인된 조건)의 "추가 설명" 이 보기 선택과 다르면 추가 설명을 따른다.**
    보기에 맞는 답이 없어 아무거나 고른 경우가 많다. 예) 보기 "항만 건설사업" + 추가 설명
    "산업단지 안에 공장을 새로 짓는 경우" → 산업단지·공장 기준으로 판단한다.
13. **기준을 말할 때 "별표 ○의 기준" 이라고만 쓰지 마라.** [조문 원문]에 있는 실제 수치(예: "면적 ○○㎡ 이상" 의 ○○ 자리 숫자)를
    적는다. 원문에 수치가 없으면 "별표 ○(제목) 원문을 확인해야 한다" 고 쓰고 무엇을 확인할지 적는다.
14. 예/아니오로 답할 질문이면 【결론】 첫마디를 판정과 맞춰라. 있으면 "예", 없으면 "아니요".
    ("네, … 없습니다" 처럼 첫마디와 판정이 어긋나면 안 된다.)
    (확인된 조건)에 "앞 질문" 이 있으면 이 질문은 그 앞 질문에 이어서 묻는 것이다. "이 경우", "그것" 은 앞 질문을 가리킨다.
15. 질문이 "**법률에** (직접) 있나요" 처럼 법령 단계를 집어 물으면 단계를 구분해 답한다.
    시행령·시행규칙·고시에만 있는 내용이면 "법률에는 없고, 「○○법 시행령」 제○조에 있습니다" 라고 쓴다.

━━ 조문 인용 규칙 ━━━━━━━━━━━━━━━━━━━━━━━━━
1. 조문번호는 [조문 원문]에서 눈으로 확인하고 적는다.
2. 법령명 + 조 + 항 + 호를 정확히 대응시킨다.
3. 항·호가 확인되지 않으면 조까지만 적는다. 추측해서 붙이지 마라.
   (제24조제1항까지만 보이면 "제24조제1항제1호" 라고 쓰지 마라.)
4. **서로 다른 호가 각각 다른 요건을 정하면 반드시 호별로 나눠 설명하라.**
   여러 호를 "○○ 목적이면 가능" 같은 한 문장으로 뭉뚱그리지 마라.
5. 조문 제목만으로 내용을 추측하지 마라. 본문을 근거로 하라.
6. **원문이 특정 항·호를 집어 예외나 다른 수치를 정하면 그 번호를 그대로 유지하라.**
   임의로 다른 범주 이름으로 바꿔 말하지 마라.
   예) 원문 "제1항제1호 및 제2호의 경우에는 100킬로그램"
       → "제1항제1호·제2호" 라고 쓴다. "배출시설 사업장 등" 으로 바꾸지 마라.
   질문의 사실관계가 그 항·호에 해당하는지는 따로 확인하거나 확정할 수 없다고 밝혀라.
7. **한 답변 안에서 같은 것을 서로 다른 항·호로 인용하지 마라.**
   앞에서 "별표 4 제2호" 라고 했다가 뒤에서 "별표 4 제1호가목" 이라고 하면
   둘 중 하나는 지어낸 것이다. 원문에서 확인되는 하나만 써라.

━━ 위임·인용 처리 ━━━━━━━━━━━━━━━━━━━━━━━━━
1. **조문을 서로 섞어 하나의 규정처럼 설명하지 마라.**
   법률·시행령·시행규칙은 각각 다른 것을 정한다.
2. 어떤 조문이 "별표 N 과 같다", "대통령령으로 정한다", "부령으로 정한다" 처럼
   **다른 곳에 넘기는 경우**, 그 조문 자체는 넘긴다는 사실만 말하라.
   구체적인 숫자를 그 조문이 정한 것처럼 쓰지 마라.
   예) 시행규칙 제12조제2항은 "누출검사주기는 별표 4와 같다" 고만 정한다.
       여기에 "매 8년" 같은 숫자를 붙이면 안 된다. 그 숫자는 별표 4 에 있다.
3. **넘겨받은 별표·조문이 [조문 원문]에 없으면 숫자를 지어내지 마라.**
   "관련 별표 원문이 확인되지 않아 구체적인 주기는 확정할 수 없습니다" 라고 쓴다.
4. **최초 검사와 그 이후 정기검사를 구분**하여 설명하라.
5. **대상 여부와 주기를 구분**하라. 조문에 제외 규정이 있으면
   (예: "「위험물안전관리법 시행령」 제17조에 따른 정기검사 대상시설을 제외한다")
   주기를 말하기 전에 대상 여부부터 짚어라.
6. ★ **별표의 "비고"·"주" 는 표의 숫자를 바꾼다. 표 값을 쓰기 전에 비고부터 읽어라.**
   비고가 작업시간·시간대·요일(공휴일)·지역·규모·측정조건에 따라 값을 **더하거나 빼거나(보정)**,
   적용 범위·예외를 정하면, 사용자 사실에 맞는 비고를 적용한 **보정 후 값**으로 판단하라.
   보정했으면 【계산】에 "표 기준 ○ + 보정 ○ (별표 ○ 비고 ○) = ○" 처럼 적는다.
   보정을 빼먹고 표 값만으로 "초과/미달" 을 판정하면 틀린 답이다.
   일요일은 공휴일이다. 사용자 사실이 비고 조건에 해당하는지 모르면 경우를 나눠 쓴다.

━━ 기간 계산 ━━━━━━━━━━━━━━━━━━━━━━━━━━━
★ **"설치 후 몇 년" 과 "마지막 검사 후 몇 년" 은 다른 것이다.**
  **조문 문구가 기준일을 정한다.** 그 문구를 찾아 【계산】에 그대로 옮겨 적어라.
    · "설치 후 5년이 지난 날부터 매 3년" → 기준일 = 설치일 + 5년.
       **직전 검사를 언제 받았든 이 기준일은 바뀌지 않는다.**
    · "완공검사를 받은 날부터 6개월 이내" → 완공검사일 기준
    · "직전 검사를 받은 날부터"           → 마지막 검사일 기준
  ★★ **조문에 "직전 검사"·"마지막 검사" 라는 말이 없으면 마지막 검사일을
     기준일로 쓰지 마라.** 사용자가 마지막 검사일을 알려줘도 마찬가지다.
     그 날짜는 "이미 받은 검사가 기한 안이었는지" 확인하는 데만 쓴다.
     (실제 사고: "설치 후 10년이 지난 날부터 매 8년" 을 직전 검사일 + 8년으로 계산)
- 사용자가 기준일이 되는 날짜를 알려줬고 조문에 주기가 있으면 **계산해서 날짜를 말하라.**
  예) 설치일 2014-04-01, 조문 "설치 후 5년이 지난 날부터 매 3년"
      → 2019-04-01 + 3년 = 2022-04-01, 그다음 2025-04-01
- 조문이 "그 해 ○월 ○일까지", "그 날부터 ○개월 이내" 처럼 별도 기한 방식을
  정하고 있으면 단순 덧셈보다 그 규정을 따른다.
- ★★ **기준일은 사용자가 알려준 날짜만 쓴다. 날짜를 지어내지 마라.**
  "설치한 지 15년 됐다" 는 **경과 연수**이지 날짜가 아니다. 여기서
  "2010년 9월 23일" 같은 날짜를 만들어 내면 안 된다. 실제 사고 사례다.
- **기준일을 모르면 계산하지 말고, 무엇을 알려주면 계산할 수 있는지 밝혀라.**
  예) "마지막 검사일을 알려주시면 다음 검사 시점을 계산할 수 있습니다."
- ★★ **연도 덧셈을 반드시 검산하라.** 2010 + 8 = 2018 이다. 2038 이 아니다.
  계산 결과를 쓰기 전에 한 번 더 더해 보라. 서버가 따로 검산해서
  틀리면 사용자에게 경고가 표시된다.
- 【적용 조건】에 적은 값과 【계산】의 기준일이 **서로 맞는지** 확인하라.
  "설치 경과 15년" 인데 기준일이 26년 전이면 둘 중 하나가 틀린 것이다.
- 주기 수치가 [조문 원문]에 없으면(별표에 있는데 별표가 안 왔으면)
  숫자를 지어내서 계산하지 마라.

━━ 위계 검토 순서 ━━━━━━━━━━━━━━━━━━━━━━━━━
① 법률   : 기본 근거, 금지·제한, 기본 요건
② 시행령 : 법률이 대통령령에 위임한 구체적 사유·대상·기간·산정방법·절차
③ 시행규칙 : 위임된 서식·세부 절차
단, [조문 원문]에 없는 단계는 만들지 마라.

━━ 읽기 쉽게 쓰기 ━━━━━━━━━━━━━━━━━━━━━━━━━
- **굵게** : 기간·주기·수량·금액 등 바로 써야 할 수치
             (예: **10년**, **6개월 이내**, **매 8년**, **90일 이내**)
- __밑줄__ : 반드시 지켜야 하는 의무·금지 (예: __받아야 한다__, __지체 없이__)
표시를 남발하지 마라. 온통 굵으면 아무것도 강조되지 않는다.
【근거】 줄의 수치에도 굵게를 쓴다.
**굵게·밑줄 외의 강조 기호를 만들어 쓰지 마라.** 특히 !! 같은 기호를
본문에 넣지 마라. 그대로 글자로 나와 법령 용어처럼 보인다.

━━ 답변 형식 ━━━━━━━━━━━━━━━━━━━━━━━━━━━
【결론】
질문에 대한 답을 __먼저__ 2~3줄로 말한다. 사용자가 다른 사람에게 그대로
읽어줄 수 있는 문장으로 쓴다. 조문번호를 나열하지 마라.
조건이 "모름" 이라 하나로 정할 수 없으면 "○○이면 A, □□이면 B" 로 쓴다.
여기서도 수치는 **굵게**, 의무는 __밑줄__ 로 표시한다.
예) 설치 전에 __관할 시장·군수·구청장에게 신고해야 합니다__.
    신고는 시설 설치 **전**에 해야 하고, 변경 시에는 **30일 이내**입니다.

【적용 조건】
사용자가 알려준 사실만 한 줄씩 적는다. 안 알려준 항목은 **줄째로 빼라**
(‑ 미확인 을 줄줄이 적으면 화면만 길어진다).
해당하는 것만 골라 쓴다: 시설 / 용량 / 설치일·설치 경과 / 검사 종류 / 마지막 검사 / 기타
예)
- 시설: 주유소 지하매설 저장시설
- 용량: 30,000L
- 설치일: 2014-04-01
- 마지막 검사: 2025-09
조건이 하나도 없으면 이 블록을 아예 쓰지 마라.

【근거】
「법령명」 제○조제○항제○호 | 그 조항이 정하는 내용 한 줄
(실제 확인된 조문만. 한 줄에 하나씩.)
★ 법령명은 반드시 「 」 로 감싼다. 대괄호 [ ] 를 쓰지 마라.
  화면이 「 」 를 보고 법제처 링크를 붙인다. 없으면 링크가 생기지 않는다.
★ 법률 → 시행령 → 시행규칙 → 별표 순으로 적는다.

【계산】
기간·기한을 물었고 **기준일과 주기가 둘 다 확인될 때만** 쓴다.
기준일 줄에는 **무엇을 기준으로 세는지와 그 조문 문구**를 함께 적는다.
  기준일 2019-04-01 (설치일 2014-04-01 + 5년 — "설치 후 5년이 지난 날부터", 「…」 별표 ○)
  + 주기 **3년** (「…」 별표 ○)
  = 다음 검사 **2022-04-01** 이후 조문이 정한 기한 안 (예: "이후 90일 이내")
기준일이나 주기 중 하나라도 확인 안 되면 이 블록을 쓰지 말고,
무엇이 필요한지만 【설명】에 한 줄 적어라.

【설명】
1. 기본 원칙 — 조문에서 확인되는 원칙. 문장 끝에 (제○조제○항) 표시.
2. 요건·예외 — 호가 여럿이면 호별로 나눠 설명.
3. 조건별 검토 — 질문에 "모름" 인 조건이 있으면 경우를 나눠 설명.
   (예: 해당하는 경우 … / 해당하지 않는 경우 …)
4. 확정할 수 없는 사항 — 정보가 부족해 특정할 수 없는 부분을 밝힌다.
   (예: "○○ 여부가 확인되지 않아 제○조제○항의 적용 여부는 확정할 수 없습니다.")

질문이 단순하고 조건이 명확하면 위 4단계를 억지로 채우지 말고
필요한 부분만 간결하게 쓴다.

분량 — 짧을수록 좋다:
- 【결론】은 3줄 안쪽. 여기만 읽어도 무엇을 해야 하는지 알 수 있어야 한다.
- 【적용 조건】·【계산】은 쓸 내용이 없으면 **블록째로 생략**한다. 빈 칸을
  "미확인" 으로 채우지 마라.
- 【근거】는 한 줄에 하나. 조문 원문을 그대로 옮기지 마라. 무엇을 정하는지 한 줄 요약.
- 【설명】은 전체 15줄 안쪽. 같은 말을 다시 쓰지 마라.
- 조문 원문은 화면 오른쪽에 이미 표시된다. 답변에 길게 재인용하지 마라.

━━ 절대 금지 ━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 조문에 없는 내용을 기억으로 보충
- 판례·행정해석을 만들어내기
- 존재하지 않는 조문번호 만들기
- 번호가 비슷하다는 이유로 다른 법령 조문 인용
- "모름" 인 조건을 하나의 경우로 확정
- 조문에 없는 일반론으로 단정
- 법률 자문 (조문이 무엇을 규정하는지만 정리한다)

【사용자 질문】
{question}

【조문 원문】
{context}
"""


def _server_root() -> str:
    """OpenAI 호환 주소에서 서버 루트 주소를 만듭니다.
    http://localhost:8080/v1  ->  http://localhost:8080
    """
    return re.sub(r"/v1/?$", "", LOCAL_BASE_URL)


_ctx_cache = {"n": 0, "t": 0.0}


def server_ctx() -> int:
    """
    llama-server 의 컨텍스트 길이(-c 값). 5분 동안 기억합니다.

    코드에 16384 를 박지 않고 서버에서 읽어서, start.bat 의 -c 만 바꾸면
    나머지(토큰 예산)가 알아서 따라가게 합니다. 못 읽으면 LOCAL_CTX(기본 16384).
    """
    if LOCAL_SERVER != "llamacpp":
        return LOCAL_CTX_FALLBACK
    now = time.time()
    if _ctx_cache["n"] and now - _ctx_cache["t"] < 300:
        return _ctx_cache["n"]
    n = 0
    try:
        d = httpx.get(f"{_server_root()}/props", timeout=5).json()
        g = d.get("default_generation_settings") or {}
        n = int(g.get("n_ctx") or d.get("n_ctx") or 0)
    except Exception:                                    # noqa: BLE001
        n = 0
    if n > 0:
        _ctx_cache.update(n=n, t=now)
        return n
    return LOCAL_CTX_FALLBACK


def count_tokens(text: str) -> int:
    """
    토큰 수. llama-server 의 /tokenize 로 정확히 셉니다 (빠릅니다, 수 ms).
    실패하면 글자 수를 그대로 씁니다 — 한국어 법령은 대개 토큰이 글자보다
    적어서, 넘치지 않는 쪽(과대추정)으로 안전합니다.
    """
    if not text:
        return 0
    if LOCAL_SERVER == "llamacpp":
        try:
            r = httpx.post(f"{_server_root()}/tokenize", json={"content": text}, timeout=30)
            toks = r.json().get("tokens")
            if isinstance(toks, list):
                return len(toks)
        except Exception:                                # noqa: BLE001
            pass
    return len(text)


def _call(prompt: str, temperature: float = 0.3, max_tokens: int = 0,
          stage: str = "llm", schema: dict | None = None) -> str:
    """LLM 한 번 호출. stage 는 로그에 찍힐 단계 이름입니다.
    schema 를 주면 서버에 JSON 형식 강제를 요청합니다 (call_json 참고)."""
    return _call_local(prompt, temperature, LOCAL_MODEL, max_tokens, stage, schema)


# ── JSON 출력 강제 (v1.31) ───────────────────────────────────
# ★ 2026-10-07 — 되묻기·선별·검색어 출력이 자유 텍스트라서 형식이 깨질 때마다
#   정규식으로 고쳐 왔습니다(되묻기 후처리만 약 600줄). llama-server 는
#   response_format 에 JSON Schema 를 주면 **문법으로 출력 형식을 강제**합니다
#   (토큰을 고를 때 스키마에 맞지 않는 토큰을 아예 못 고르게 함).
#   서버가 이 옵션을 거절하면(구버전·다른 서버) 한 번 끄고 예전 텍스트 방식으로
#   돌아갑니다. 호출하는 쪽은 결과가 None 이면 기존 파서를 씁니다.
#   LLM_JSON=0 이면 처음부터 끕니다.
_JSON_MODE = {"ok": os.getenv("LLM_JSON", "1") not in ("0", "false", "False", "")}


def json_enabled() -> bool:
    return bool(_JSON_MODE["ok"])


def _parse_json(text: str):
    """모델 응답에서 JSON 객체 하나를 꺼냅니다. 실패하면 None."""
    t = re.sub(r"<think>[\s\S]*?</think>", "", str(text or ""), flags=re.I)
    t = re.sub(r"```[a-zA-Z]*", "", t).replace("```", "").strip()
    try:
        return json.loads(t)
    except (ValueError, TypeError):
        pass
    m = re.search(r"\{[\s\S]*\}", t)            # 앞뒤에 말이 붙은 경우
    if m:
        try:
            return json.loads(m.group(0))
        except (ValueError, TypeError):
            return None
    return None


def call_json(prompt: str, schema: dict, temperature: float = 0,
              max_tokens: int = 0, stage: str = "llm"):
    """
    JSON 형식을 강제해 호출하고 파싱된 객체를 돌려줍니다.
    반환: (obj 또는 None, 원문 텍스트). JSON 모드가 꺼져 있으면 (None, "") —
    호출하는 쪽이 예전 텍스트 프롬프트로 다시 부릅니다.
    """
    if not json_enabled():
        return None, ""
    raw = _call(prompt, temperature=temperature, max_tokens=max_tokens,
                stage=stage, schema=schema)
    obj = _parse_json(raw)
    if obj is None:
        applog.warn(f"LLM {stage}: JSON 파싱 실패 — 텍스트 방식으로 대체합니다. 앞부분: {raw[:120]}")
    return obj, raw


# ── v1.33 — 로컬 LLM 자동 재시작 (워치독) ─────────────────────────
# 2026-10-08 새벽: llama-server 가 메모리 부족으로 두 번 꺼지고, 한 번은 로그 창이 멈춰
# (콘솔 "빠른 편집" 선택) 응답 없이 매달렸습니다. 사람이 없으면 평가·서비스가 그대로 멈춥니다.
# 실제 LLM 호출이 LLM_RESTART_GRACE 초 넘게 계속 실패(연결 안 됨·시간 초과)하면
# lawfinder-llm 창과 llama-server 를 정리하고 _5_restart_llm.bat 으로 다시 켭니다.
# Windows 에서만 동작합니다. .env 에 LLM_AUTO_RESTART=0 이면 끕니다. 10분에 한 번까지만.
LLM_AUTO_RESTART = os.getenv("LLM_AUTO_RESTART", "1") not in ("0", "false", "False")
LLM_RESTART_GRACE = float(os.getenv("LLM_RESTART_GRACE", "180"))
_LLM_STATE = {"first_fail": 0.0, "last_restart": 0.0, "fails": 0}


def _llm_health(ok: bool, why: str = "") -> None:
    if ok:
        _LLM_STATE["first_fail"], _LLM_STATE["fails"] = 0.0, 0
        return
    now = time.time()
    _LLM_STATE["fails"] += 1
    if not _LLM_STATE["first_fail"]:
        _LLM_STATE["first_fail"] = now
        return
    if (now - _LLM_STATE["first_fail"] >= LLM_RESTART_GRACE and _LLM_STATE["fails"] >= 2
            and now - _LLM_STATE["last_restart"] >= 600):
        _LLM_STATE["last_restart"] = now
        _LLM_STATE["first_fail"], _LLM_STATE["fails"] = 0.0, 0
        restart_local_llm(f"{why} 상태가 {LLM_RESTART_GRACE:.0f}초 넘게 계속됨")


def restart_local_llm(reason: str = "") -> bool:
    """lawfinder-llm 창(과 그 안의 llama-server·logpipe)을 정리하고 _5_restart_llm.bat 으로 다시 켭니다."""
    if os.name != "nt" or not LLM_AUTO_RESTART or LOCAL_SERVER != "llamacpp":
        return False
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    bat = os.path.join(here, "_5_restart_llm.bat")
    if not os.path.exists(bat):
        applog.warn(f"LLM 자동 재시작 불가 — {bat} 없음")
        return False
    applog.warn(f"LLM 자동 재시작: {reason}")
    for cmd in (["taskkill", "/F", "/T", "/FI", "WINDOWTITLE eq lawfinder-llm*"],
                ["taskkill", "/F", "/IM", "llama-server.exe"]):
        try:
            subprocess.run(cmd, capture_output=True, timeout=30)
        except Exception as e:                       # noqa: BLE001
            applog.warn(f"LLM 자동 재시작 — {' '.join(cmd[:2])} 실패: {e}")
    time.sleep(3)
    try:
        subprocess.Popen(["cmd", "/c", bat], cwd=here,
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        return True
    except Exception as e:                           # noqa: BLE001
        applog.warn(f"LLM 자동 재시작 실패: {e}")
        return False


def _call_local(prompt: str, temperature: float, model: str,
                max_tokens: int = 0, stage: str = "llm",
                schema: dict | None = None, _wait: int = 0) -> str:
    """
    로컬 LLM 에 요청합니다.

    LOCAL_SERVER=ollama 이고 LOCAL_NO_THINK=1 이면 Ollama 네이티브 API(/api/chat)를
    쓰고 think=false 를 보냅니다. 그 외(llama-server 포함)에는 OpenAI 호환
    /chat/completions 를 씁니다.

    ★ 2026-09-29 — llama-server 는 컨텍스트를 넘는 요청을 잘라서 실행하지 않고
      400 으로 거절합니다(예전 Ollama 는 **앞부분 지시문을 잘라낸 채** 조용히
      실행했습니다). 보내기 전에 토큰을 세서
        · 입력이 컨텍스트를 넘으면 → 보내지 않고 이유를 알립니다
        · 출력 상한이 남은 자리보다 크면 → 남은 만큼으로 줄입니다
      (출력도 같은 컨텍스트를 씁니다. 줄이지 않으면 답변 도중에 끊깁니다)
    """
    if not model:
        if LOCAL_SERVER == "llamacpp":
            model = "local"          # llama-server 는 모델 하나만 서비스하므로 이름은 무시됩니다
        else:
            raise AiError("LOCAL_MODEL 이 비어 있습니다. .env 에 Ollama 모델 이름을 넣으세요.")

    n_prompt = None
    if LOCAL_SERVER == "llamacpp":
        n_prompt = count_tokens(prompt) + 32             # 채팅 템플릿이 붙이는 토큰 여유
        ctx = server_ctx()
        room = ctx - n_prompt
        if room < 64:
            applog.llm(stage, n_prompt, 0, 0.0, note="컨텍스트 초과 — 보내지 않음", sent=False)
            raise AiError(
                f"프롬프트({n_prompt:,}토큰)가 로컬 LLM 컨텍스트({ctx:,}토큰)를 넘습니다. "
                f"llama-server 실행 옵션의 -c 값을 늘리거나 질문 범위를 좁혀 주세요.")
        if not max_tokens or max_tokens > room - 16:
            max_tokens = room - 16

    native = LOCAL_NO_THINK and LOCAL_SERVER == "ollama"
    if native:
        url = f"{_server_root()}/api/chat"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "think": False,          # ★ 사고 과정 끄기
            "stream": False,
            "options": {
                "temperature": temperature,
                # num_predict 가 없으면 무제한 생성됩니다.
                **({"num_predict": max_tokens} if max_tokens else {}),
            },
        }
        if schema and json_enabled():
            payload["format"] = schema          # Ollama 는 format 에 스키마를 받습니다
    else:
        url = f"{LOCAL_BASE_URL}/chat/completions"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "stream": False,
            **({"max_tokens": max_tokens} if max_tokens else {}),
        }
        if LOCAL_NO_THINK and LOCAL_SERVER == "llamacpp":
            # llama-server 를 --jinja 로 띄웠을 때 채팅 템플릿에 전달됩니다.
            # 안 먹으면 llama-server 실행 옵션에 --reasoning-budget 0 을 추가하세요.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if schema and json_enabled():
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": re.sub(r"\W", "_", stage) or "out",
                                "schema": schema, "strict": True},
            }

    applog.debug(stage, f"── 프롬프트 ({n_prompt or '?'} tok, max_tokens={max_tokens}) ──\n{prompt}")
    t0 = time.time()
    who = "Ollama" if LOCAL_SERVER == "ollama" else "llama-server"
    try:
        resp = httpx.post(url, json=payload, timeout=LOCAL_TIMEOUT)
        resp.raise_for_status()
    except httpx.ConnectError as e:
        msg = f"로컬 LLM 서버에 연결하지 못했습니다 ({url}). {who} 가 실행 중인지 확인하세요."
        applog.llm(stage, n_prompt, None, time.time() - t0, note=f"실패: {who} 연결 안 됨")
        _llm_health(False, "연결 안 됨")
        raise AiError(msg) from e
    except httpx.TimeoutException as e:
        applog.llm(stage, n_prompt, None, time.time() - t0, note="실패: 시간 초과")
        _llm_health(False, "시간 초과")
        raise AiError(f"로컬 LLM 응답이 {LOCAL_TIMEOUT:.0f}초 안에 오지 않았습니다.") from e
    except httpx.HTTPStatusError as e:
        code, body = e.response.status_code, e.response.text[:300]
        applog.llm(stage, n_prompt, None, time.time() - t0, note=f"실패: HTTP {code}")
        applog.warn(f"LLM {stage} HTTP {code}: {body}")
        # ★ v1.33 — 재시작 직후 모델을 올리는 중(503 "Loading model")이면 5초씩, 최대 2분 기다립니다.
        if code == 503 and "loading" in body.lower() and _wait < 24:
            time.sleep(5)
            return _call_local(prompt, temperature, model, max_tokens, stage, schema, _wait + 1)
        if code == 404:
            if LOCAL_SERVER == "llamacpp":
                raise AiError(
                    f"llama-server 주소가 맞지 않습니다 ({url}). .env 의 LOCAL_BASE_URL 이 "
                    f"http://localhost:8080/v1 처럼 /v1 로 끝나는지 확인하세요.") from e
            raise AiError(f"모델 '{model}' 을(를) 찾지 못했습니다. "
                          f"'ollama list' 로 설치된 이름을 확인하세요.") from e
        if code == 400 and "context" in body.lower():
            raise AiError(
                "프롬프트가 로컬 LLM 컨텍스트 길이를 넘었습니다. "
                "llama-server 실행 옵션의 -c 값을 늘리세요(예: -c 24576).") from e
        if code in (400, 422) and schema and json_enabled():
            # 서버가 JSON 형식 강제를 못 받는 경우 — 끄고 같은 요청을 한 번 다시 보냅니다.
            _JSON_MODE["ok"] = False
            applog.warn(f"LLM 서버가 JSON 형식 강제를 받지 않아 끕니다 (HTTP {code}). "
                        f"이후 텍스트 방식으로 동작합니다.")
            return _call_local(prompt, temperature, model, max_tokens, stage, None)
        if code == 400 and native:
            raise AiError("이 서버가 think 옵션을 받지 않습니다. "
                          ".env 에서 LOCAL_NO_THINK=0 으로 두고 다시 시도하세요.") from e
        raise AiError(f"로컬 LLM 오류 ({code}): {body}") from e
    except httpx.HTTPError as e:
        applog.llm(stage, n_prompt, None, time.time() - t0, note=f"실패: {type(e).__name__}")
        raise AiError(f"로컬 LLM 호출 실패: {e}") from e

    secs = time.time() - t0
    _llm_health(True)
    data = resp.json()
    thinking = ""
    if native:                                   # /api/chat 응답 구조
        msg = data.get("message") or {}
        text = msg.get("content") or ""
        thinking = msg.get("thinking") or ""
    else:                                        # /chat/completions 응답 구조
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        text = msg.get("content") or ""
        thinking = msg.get("reasoning_content") or msg.get("reasoning") or ""
    if not text.strip():
        text = thinking

    # 사용량·속도 (llama-server: usage/timings, Ollama: *_count)
    usage = data.get("usage") or {}
    pt = usage.get("prompt_tokens") or data.get("prompt_eval_count") or n_prompt
    ct = usage.get("completion_tokens") or data.get("eval_count")
    tps = (data.get("timings") or {}).get("predicted_per_second")
    done = data.get("done_reason") if native else \
        ((data.get("choices") or [{}])[0].get("finish_reason"))
    finish = "length" if done in ("length", "MAX_TOKENS") else (done or "")
    note = ""
    if thinking and LOCAL_NO_THINK:
        # 생각 모드를 껐는데도 생각 과정이 나왔습니다. 느려지는 주원인입니다.
        note = f"생각 과정 {len(thinking):,}자 생성됨(끄기 설정이 안 먹음)"
    applog.llm(stage, pt, ct, secs, finish=finish, tps=tps, note=note)

    if not text.strip():
        raise AiError(f"로컬 LLM 이 빈 응답을 돌려줬습니다: {str(data)[:400]}")

    raw = text
    if schema and json_enabled():
        # JSON 응답은 마크다운 정리(_clean_output)를 거치면 깨질 수 있습니다.
        applog.debug(stage, f"── 응답 원문 JSON (finish={done}) ──\n{raw}")
        return raw.strip()
    text = _clean_output(text)
    applog.debug(stage, f"── 응답 원문 (finish={done}) ──\n{raw}"
                 + (f"\n── 생각 과정 ──\n{thinking}" if thinking else "")
                 + f"\n── 정리 후 ──\n{text}")
    return text


# ── 응답 정리 ────────────────────────────────────────────────
def _clean_output(text: str) -> str:
    """
    로컬 모델이 덧붙이는 것들을 걷어냅니다.

    작은 모델은 형식을 지키라고 해도 사고 과정·마크다운·코드펜스를 섞어 냅니다.
    파싱이 깨지지 않도록 여기서 정리합니다.
    """
    t = str(text or "")
    # 사고 과정 블록
    t = re.sub(r"<think>[\s\S]*?</think>", "", t, flags=re.I)
    t = re.sub(r"<\|?thinking?\|?>[\s\S]*?<\|?/?thinking?\|?>", "", t, flags=re.I)
    # 닫히지 않은 <think> 는 그 뒤를 전부 사고 과정으로 봅니다
    t = re.sub(r"<think>[\s\S]*$", "", t, flags=re.I)
    # 코드펜스
    t = re.sub(r"```[a-zA-Z]*\n?", "", t)
    t = t.replace("```", "")

    # ── 마크다운 정리 ────────────────────────────────────────────
    # ★ 2026-08-18 — 여기서 `**굵게**` 를 **무조건 지우고 있었습니다.**
    #   원래 의도는 용어추출·되묻기 단계에서 모델이 `**법령명:**` 처럼 라벨을
    #   감싸 파싱이 깨지는 것을 막는 것이었는데, 이 함수는 **답변에도 똑같이**
    #   적용됩니다. 그래서 프롬프트로 "수치는 굵게, 의무는 밑줄" 을 시키고
    #   화면에도 `**`→<b>, `__`→<u> 변환이 멀쩡히 있는데도, 그 사이에서
    #   마커가 전부 지워져 **강조가 한 번도 화면에 안 나왔습니다.**
    #   (v1.7 에서 "지시가 실제로 안 들어갔다"고 고쳤던 것과 같은 자리인데,
    #    이번엔 지시는 들어갔고 출력 정리 단계에서 지워지고 있었습니다.)
    #
    #   답변인지 아닌지는 【근거】/【설명】/【결론】 머리표로 구분합니다.
    #   용어추출·되묻기·조문선택 응답에는 이 머리표가 없습니다.
    #   ★ 2026-08-19 — 머리표를 굵게 감싸는 일이 잦습니다(`**【결론】**`).
    #     그대로 두면 화면에서 `**`→<b> 가 먼저 돌아 `<b>【결론】</b>` 이 되고,
    #     그 다음 소제목 정규식 `^【…】$` 가 안 걸려 **결론 상자와 모든
    #     소제목이 통째로 사라집니다.** 판정 전에 벗겨냅니다.
    t = re.sub(r"\*\*[ \t]*(【[^】\n]{1,12}】)[ \t]*\*\*", r"\1", t)

    is_answer = any(k in t for k in ("【근거】", "【설명】", "【결론】"))
    if is_answer:
        # 답변 — 강조와 글머리표를 살립니다.
        t = re.sub(r"^\s*#+\s*", "", t, flags=re.M)        # 제목(#) 만 제거
        t = re.sub(r"^\s*>+\s*", "", t, flags=re.M)        # 인용부호(>) 제거
        t = re.sub(r"^(\s*)\*\s+", r"\1- ", t, flags=re.M)  # * 글머리표 → -
        # 조문번호에 굵게가 끼면 화면의 조문 링크 정규식이 깨집니다.
        # (`제**12**조` → "제12조" 로 못 읽음). 그 자리만 걷어냅니다.
        t = re.sub(r"제\s*\*\*(\d+)\*\*\s*(조|항|호|목)", r"제\1\2", t)
    else:
        # 파싱 대상 응답 — 마커가 있으면 형식이 깨지므로 전부 걷어냅니다.
        t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
        t = re.sub(r"^\s*[#>\-\*]+\s*", "", t, flags=re.M)
    # 전각 콜론·쉼표를 반각으로
    t = t.replace("：", ":").replace("，", ",")

    # ★ 숫자 뒤에 끼는 공백을 붙입니다.
    #   로컬 모델(Qwen)은 "제8 조제1 항제1 호", "매 8 년", "90 일 이내",
    #   "2 만 리터" 처럼 숫자와 뒷글자 사이를 띄웁니다.
    #
    #   읽기만 나쁜 것이 아닙니다. 화면의 조문 링크 정규식이 "제\d+조" 를
    #   찾는데 "제8 조" 는 걸리지 않아, **답변의 하이퍼링크가 전부 사라집니다.**
    #   실제로 그렇게 됐습니다. 서버의 인용 검증 정규식은 공백을 허용해서
    #   한쪽만 동작하고 있었습니다.
    #   ★ 2026-08-19 — `\s` 대신 `[ \t]` 를 씁니다. `\s` 는 줄바꿈도 먹어서
    #     숫자로 끝난 줄이 다음 줄과 통째로 붙었습니다. 실측 사례:
    #         「…시행규칙」 별표 4
    #         일반기준에 따라 매 8년마다 …
    #       → "「…시행규칙」 별표 4일반기준에 따라 매 8년마다 …"
    #     근거 두 줄이 한 줄로 뭉개지고, 별표 번호와 수치가 같은 문장에
    #     들어가 버려서 main.py 의 별표 경고까지 **멀쩡한 답변에** 떴습니다.
    t = re.sub(r"(제[ \t]*\d+)[ \t]+(조|항|호|목|장|절|편|관)", r"\1\2", t)
    t = re.sub(r"(\d)[ \t]+(년|월|일|개월|주일|시간|분|초|회|차|건|명|배|"
               r"천|만|억|리터|킬로그램|킬로|그램|톤|퍼센트|미터|제곱미터)",
               r"\1\2", t)
    return t.strip()


# 라벨 표기 흔들림 대응. 모델마다 "법령명" 을 "법령", "법률명" 등으로 씁니다.
_LABEL_LAW = ("법령명", "법령", "법률명", "법률", "law")
_LABEL_TERM = ("용어", "법령용어", "키워드", "term", "keyword")


# 프롬프트의 양식 자리표시자나 모델의 사고 과정 조각이 섞여 들어오는 것을 막습니다.
# 실제로 관측된 예: "용어1", "LawA", "Term3", "용어2 (Wait", "`누출검사`."
_JUNK_RE = re.compile(
    r"^(용어|법령명|term|law|keyword|보기|option|item)\s*\d*$"   # 자리표시자
    r"|^\d+$"                                                   # 숫자만
    r"|^[a-zA-Z\s]+$"                                           # 영어만
    r"|wait|but |usually|related|however|note:|example",          # 사고 과정 조각
    re.I,
)


def _is_junk(s: str) -> bool:
    s = s.strip()
    if not s or len(s) > 40 or len(s) < 2:
        return True
    if _JUNK_RE.search(s):
        return True
    # 한글이 하나도 없으면 법령 용어가 아닙니다.
    if not re.search(r"[가-힣]", s):
        return True
    return False


def _looks_like_law(s: str) -> bool:
    """'토양환경보전법 시행령' 처럼 법령 이름으로 보이는지."""
    s = s.strip()
    if not s:
        return False
    # ★ 2026-08-19 — "…방법/용법/기법" 이 '법' 으로 끝난다는 이유로 법령명으로
    #   승격돼, "누출검사방법" 같은 용어로 lsStmd 를 조회하고 있었습니다.
    #   헛 조회로 끝나긴 하지만 용어 목록에서 빠져 검색 품질이 떨어집니다.
    if re.search(r"(방법|용법|기법|공법|수법|요법)$", s):
        return False
    # ★ 2026-09-29 — "…에 관한 법률" 은 '률' 로 끝나서 법령명으로 안 잡혔습니다.
    #   되묻기 보기에 "광산피해의 방지 및 복구에 관한 법률" 이 그대로 나갔습니다.
    return bool(re.search(r"(법|법률|령|규칙|조례|고시|지침|예규|훈령)$", s))


def extract_terms(question: str) -> dict:
    """
    질문 -> 검색어 후보. 반환: {"법령명": [...], "용어": [...]}

    로컬 모델은 형식을 자주 어깁니다. 라벨이 있으면 그것을 쓰고,
    없으면 줄 단위로 훑어 법령처럼 생긴 것과 아닌 것을 나눕니다.
    """
    out = {"법령명": [], "용어": []}

    # ★ v1.31 — JSON 형식 강제를 먼저 시도합니다. 안 되면 예전 텍스트 방식.
    obj, _ = call_json(TERM_PROMPT.format(question=question, output_rules=_TERM_RULES_JSON),
                       TERM_SCHEMA, temperature=0, max_tokens=MAXTOK_TERMS, stage="terms")
    if isinstance(obj, dict):
        raw = ("법령명: " + ", ".join(str(x) for x in (obj.get("laws") or []) if x) + "\n"
               + "용어: " + ", ".join(str(x) for x in (obj.get("terms") or []) if x))
    else:
        raw = _call(TERM_PROMPT.format(question=question, output_rules=_TERM_RULES_TEXT),
                    temperature=0, max_tokens=MAXTOK_TERMS, stage="terms")

    def add(key: str, chunk: str):
        # ★ 2026-09-29 — 가운뎃점(·)으로는 나누지 않습니다. "소음·진동관리법" 이
        #   "소음" / "진동관리법" 으로 쪼개져 엉뚱한 법을 찾았습니다.
        for item in re.split(r"[,、/]|\s{2,}", chunk):
            item = item.strip().strip("\"'“”‘’[]()「」『』`.")
            item = re.sub(r"^\d+[.)]\s*", "", item)      # "1. " 같은 번호 제거
            item = re.sub(r"\s*\(.*$", "", item)          # "용어2 (Wait" 같은 꼬리 제거
            item = item.strip()
            if item and item not in out[key] and not _is_junk(item):
                out[key].append(item)

    # 1) 라벨이 붙은 줄
    for line in raw.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        label, _, rest = line.partition(":")
        label = label.strip().lower()
        if any(label.startswith(x) for x in _LABEL_LAW):
            add("법령명", rest)
        elif any(label.startswith(x) for x in _LABEL_TERM):
            add("용어", rest)

    # 2) 라벨이 하나도 없으면 줄 단위로 추정합니다.
    if not out["법령명"] and not out["용어"]:
        for line in raw.splitlines():
            line = line.strip().strip("-*· ")
            line = re.sub(r"^\d+[.)]\s*", "", line)
            if not line or len(line) > 60:
                continue
            for item in re.split(r"[,、/]", line):
                item = item.strip().strip("`\"'“”[]()")
                if _is_junk(item):
                    continue
                key = "법령명" if _looks_like_law(item) else "용어"
                if item not in out[key]:
                    out[key].append(item)

    # 3) 용어만 나왔는데 그중 법령처럼 생긴 게 있으면 옮깁니다.
    if not out["법령명"]:
        movers = [t for t in out["용어"] if _looks_like_law(t)]
        for m in movers:
            out["용어"].remove(m)
            out["법령명"].append(m)

    # 법령명 자리에 법령처럼 생기지 않은 것이 들어왔으면 용어로 내립니다.
    bad = [x for x in out["법령명"] if not _looks_like_law(x)]
    for b in bad:
        out["법령명"].remove(b)
        if b not in out["용어"]:
            out["용어"].append(b)

    out["법령명"] = out["법령명"][:3]
    out["용어"] = out["용어"][:6]

    if LLM_DEBUG:
        _dbg(f"[terms] 파싱 결과: {out}")

    return out


def _tidy_answer(text: str) -> str:
    """
    v1.32 — 답변 마무리 손질 (내용은 바꾸지 않습니다).
      · 일본어 조사가 섞여 나오는 것("보호위원회 등への 신고")을 한국어로.
      · 【적용 조건】 블록의 "…: 미확인" 줄은 지웁니다(지시문이 빼라고 한 줄). 블록이 비면 머리표도.
    """
    t = str(text or "")
    t = re.sub(r"(?<=[가-힣\s])への", "에 대한", t)
    t = re.sub(r"(?<=[가-힣])での(?=[\s가-힣])", "에서의", t)
    t = re.sub(r"(?<=[가-힣])の(?=[\s가-힣])", "의", t)
    if re.search(r"[぀-ヿ]", t):
        _dbg("[answer] 일본어 문자가 남아 있습니다: "
             + ", ".join(sorted(set(re.findall(r"[぀-ヿ]+", t))))[:60])
    m = re.search(r"【적용 조건】[ \t]*\n([\s\S]*?)(?=\n[ \t]*【|\Z)", t)
    if m:
        body = m.group(1)
        kept = [ln for ln in body.split("\n")
                if not re.match(r"^\s*[-·•]?\s*[^:\n]{1,30}:\s*(미확인|확인\s*안\s*됨|불명|알\s*수\s*없음)\s*$", ln)]
        new_body = "\n".join(kept)
        if new_body != body:
            if new_body.strip():
                t = t[:m.start(1)] + new_body + t[m.end(1):]
            else:
                t = t[:m.start()] + t[m.end():].lstrip("\n")
    return t


def answer(question: str, context: str) -> str:
    """조문 원문을 근거로 답변 생성."""
    return _tidy_answer(_call(ANSWER_PROMPT.format(question=question, context=context),
                              max_tokens=MAXTOK_ANSWER, stage="answer"))


CLARIFY_PROMPT = """너는 법령 질문에 답하기 전에, 결론을 가르는 사실 중 빠진 것만 되묻는 도구다.
질문자는 법령에 익숙하지 않다. 무엇을 알려줘야 결론이 정해지는지 스스로 모른다.

[관련 조문] 은 이 질문에 답하려고 실제로 고른 조문과 별표의 **본문**이다.
**이 본문에 적힌 요건·기준값만 근거로 질문하라.**
  · 조문이 "~인 경우", "~이상", "~이내", "다만 ~" 처럼 **갈래를 나누는 지점**을 찾는다.
  · 그 갈래를 정하는 사실이 [질문]·[이미 답변된 조건]에 없을 때만 묻는다.
  · **보기는 조문에 적힌 기준값을 경계로 나눈다.** 예) 조문이 "총 용량 2만리터 이상" 이면
    "2만 리터 미만 / 2만 리터 이상 / 모름". 조문에 없는 경계를 지어내지 마라.

절대 지킬 것:
- **[관련 조문]에 없는 제도·시설·용어를 질문에 쓰지 마라.**
  네 기억에 있는 다른 법의 개념을 끌어오지 마라.
  예) 관련 조문이 토양환경보전법뿐인데 "VOC 배출시설", "유해화학물질 취급시설" 을
      선택지로 넣으면 안 된다. 그것은 다른 법의 용어다.
- 용어는 조문에 적힌 **정식 명칭 그대로** 쓴다.
  예) "특정토양오염유발시설"(X) → "특정토양오염관리대상시설"(O)
- 조문에서 근거를 찾을 수 없는 구분은 묻지 마라.
- **어느 법령이 적용되는지 묻지 마라. 법령 이름을 질문이나 보기로 쓰지 마라.**
  질문자는 법을 모른다. 어느 법인지 가리는 것은 이 도구가 할 일이다.
  나쁜 예) 토양환경보전법|광산피해의 방지 및 복구에 관한 법률|모름
  대신 그 법들을 가르는 **현장 사실**을 물어라.
  좋은 예) 오염이 발생한 곳이 어디입니까?|주유소|광산|모름

━━ 무엇을 물을지 고르는 순서 ━━━━━━━━━━━━━━━━━━━
아래 A→D 순서로 따진다. **A 가 비어 있으면 A 부터 묻는다.**
  A. 이 시설·행위가 그 법령의 **적용대상인지** 가르는 조건
  B. 의무(검사·허가·신고)가 **발생하는지** 가르는 조건
  C. 의무의 **주기·기한**을 가르는 조건
  D. **예외·제외** 규정을 적용할지 가르는 조건
법적 결론이 달라지지 않는 것은 묻지 않는다.
가장 결정적인 것을 **맨 앞 줄**에 둔다.

━━ 날짜·주기를 묻는 질문일 때 ━━━━━━━━━━━━━━━━━
질문이 "언제까지", "몇 년마다", "다음 검사는 언제", "주기가 어떻게" 처럼
**기간**을 묻는 것이면, 계산에 쓸 **날짜 사실**을 확보한다.
  · 설치일 (또는 완공검사일·사용 개시일)
  · 마지막으로 검사를 받은 날
어느 날을 기준으로 세는지는 **조문마다 다르다** (설치일부터 세는 조문도 있고
직전 검사일부터 세는 조문도 있다). 그 판단은 답변 단계에서 조문을 보고 한다.
그러므로 위 두 날짜 중 [질문]·[이미 답변된 조건]에 **없는 것만** 묻는다.

★ "설치한 지 15년 됐다" 는 경과 연수이지 날짜가 아니다.
  기간 계산이 필요하면 실제 날짜를 물어라.

★ 날짜는 보기로 만들 수 없다. 화면에 자유 입력 칸이 따로 있으므로
  보기를 이렇게 만든다.
      설치일이 언제입니까?|아래 칸에 날짜 입력|모름
      마지막 누출검사를 언제 받았습니까?|아래 칸에 날짜 입력|받은 적 없음|모름

━━ 결론을 되묻지 마라 ━━━━━━━━━━━━━━━━━━━━━━━━
★★ **기한·주기·의무 여부·법정 대상 해당 여부는 이 도구가 조문으로 알아낼
  결론이다. 질문자에게 묻지 마라.** 대신 그 결론을 가르는 **현장 사실**을 물어라.
  나쁜 예) 다음 정기 누출검사를 언제까지 받아야 합니까?   ← 질문자가 물은 것 그 자체
           특정토양오염관리대상시설에 해당하나요?          ← 용량·물질로 조문이 정하는 것
           방지시설을 설치해야 합니까?                    ← 답변이 알려줄 의무
  좋은 예) 같은 부지 저장시설의 총 용량은 얼마입니까?|2만L 미만|2만L 이상|모름
           설치일이 언제입니까?|아래 칸에 날짜 입력|모름
  (물질·시설의 성질 자체 — 예: 방사성폐기물인지 — 는 현장 사실이므로 물어도 된다.)

{output_rules}

보기를 만들 때 반드시 지킬 것:
- 보기는 **질문자가 현장에서 아는 사실**이어야 한다.
  좋은 예) 지하매설 저장시설 / 지상·옥내 저장시설 / 모름
           10년 미만 / 10년 이상 / 모름
           석유류 / 유해화학물질 / 모름
- **"확인 필요", "검토 요망", "여부 확인" 같은 할 일을 보기로 만들지 마라.**
  나쁜 예) 제3조에 따른 제외 여부 확인 필요   ← 이것은 답이 아니라 할 일이다
           면제 신청 여부 확인 필요
- 보기에 조문 번호를 쓰지 마라. 질문자는 조문을 모른다.
  나쁜 예) 시행규칙 제8조의2 면제 등
- ★ **"별표 3 기준 이상/미만" 처럼 별표·기준을 가리키는 보기를 쓰지 마라.** 질문자는 그 기준이 무엇인지 모른다.
  [관련 조문]·별표에 적힌 **실제 수치**로 나눠라. 예) (별표에 "총 용량 2만 리터 이상" 이 있으면) 2만 리터 미만 / 2만 리터 이상 / 모름
  수치를 확인할 수 없으면 보기를 "아래 칸에 직접 입력 / 모름" 으로 둔다.
- **보기는 질문이 묻는 것과 같은 종류여야 한다.** "얼마입니까" 를 물었으면 보기는 수치(범위)다.
- 범주를 고르게 할 때 보기가 모든 경우를 덮지 못하면 "기타" 를 넣어라. 마지막 보기는 항상 "모름".

- **"…에 해당하나요?" 처럼 한쪽만 주지 마라. 반대쪽도 반드시 넣어라.**
  한쪽만 주면 사용자가 "아니다" 를 고를 수 없다.
  나쁜 예) 방사성폐기물에 해당하나요?|방사성폐기물|모름
  좋은 예) 방사성폐기물에 해당하나요?|방사성폐기물|아니오|모름
  (구체적인 말이 있으면 "예" 보다 그 말을 쓴다. 무엇에 예인지 바로 보인다.)

규칙:
- 한 번에 최대 3개까지 물을 수 있다.
  많이 묻는 것보다 **결정적인 것을 먼저 묻는 것**이 중요하다.
- 보기는 2~4개. 각 보기는 14자 이내. 마지막 보기로 "모름" 을 넣어라.
- 실제로 적용 조문·기준·기한이 달라지는 것만 묻는다.
- **질문자가 준 측정값·수치는 그대로 믿고 쓴다.** 측정 방법·측정 기관·측정 지점처럼
  절차의 적법성은 묻지 마라.
- **질문 상황에서 당연히 성립하는 전제는 묻지 마라.** 예외·제외 규정은 질문 내용과 정면으로
  관련될 때만 묻는다. 예) "주거지역 아파트 공사장" 인데 "300m 안에 주택이 있습니까?" 를 묻지 마라.
- **질문이 묻는 그 의무·기준에 대해서만** 묻는다. [관련 조문]에 다른 의무가 함께 있어도
  그 요건은 묻지 마라. 예) "정보주체에게 언제까지 알려야 하나" 를 물었는데
  기관 **신고** 요건(1천명 이상·민감정보 등)을 묻지 마라 — 통지 기한은 그것과 무관하다.
- [이미 답변된 조건] 에 나온 것은 절대 다시 묻지 마라. 다르게 표현해서도 묻지 마라.
- **[질문] 본문에 이미 적힌 사실(날짜·용량·지역·시간·횟수)도 다시 묻지 마라.**
- 남은 갈래가 없으면 더 묻지 말고 끝내라 (출력 형식의 "물을 것 없음" 표시).
  남은 것이 없는데 계속 묻는 것이 더 나쁘다. 갈래가 2개뿐이면 묻지 않아도 된다 —
  답변이 두 경우를 나눠 설명할 수 있다.

[질문]
{question}

[이미 답변된 조건]
{answered}

[관련 조문]
{catalog}
"""

_CLARIFY_RULES_TEXT = """더 물을 것이 없으면 딱 한 줄만 출력한다.
OK

물을 것이 남았으면 아래 형식으로만 출력한다. 다른 말은 쓰지 않는다. 한 줄에 하나.
질문|보기1|보기2|보기3"""

_CLARIFY_RULES_JSON = """JSON 하나만 출력한다.
먼저 "known" 에 [질문]·[이미 답변된 조건]에 **이미 적힌 사실**을 짧게 옮겨 적는다.
  예) ["측정 시각 오후 3시", "장비 사용 하루 2시간", "주거지역", "평일"]
그다음 "questions" 를 만든다. **known 에 적은 사실로 정해지는 갈래는 묻지 않는다.**
  물을 것이 없으면:  {"known": [...], "done": true, "questions": []}
  물을 것이 있으면:  {"known": [...], "done": false, "questions": [{"question": "질문", "options": ["보기1", "보기2", "모름"]}]}"""

CLARIFY_SCHEMA = {
    "type": "object",
    "properties": {
        # ★ v1.32 — 질문에 이미 있는 사실을 **먼저** 적게 합니다(구조화된 출력 안의 짧은 추론).
        #   실제 사례: "평일 오후 3시 · 하루 2시간 · 주거지역" 이라고 적힌 질문에
        #   측정 시각·사용 시간·지역을 3라운드에 걸쳐 되물었고, 답변까지 꼬였습니다.
        "known": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "done": {"type": "boolean"},
        "questions": {
            "type": "array", "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"},
                                "minItems": 2, "maxItems": 4},
                },
                "required": ["question", "options"],
            },
        },
    },
    "required": ["known", "done", "questions"],
}

# ── v1.32 — 질문 원문에 이미 있는 사실을 되묻는 항목 거르기 (마지막 그물) ──────────
#   프롬프트·known 으로 1차로 막고, 그래도 나오면 여기서 버립니다. 표면 신호만 봅니다.
_Q_TIME_OF_DAY = re.compile(r"(오전|오후|새벽|아침|저녁|밤|낮)\s*\d{1,2}\s*시(?!간)|정오|자정|\d{1,2}\s*시\s*(\d{1,2}\s*분)?\s*(에|경|쯤|무렵)")
_Q_DURATION = re.compile(r"\d+(\.\d+)?\s*(시간|분간)")
_Q_ZONE = re.compile(r"(주거|상업|공업|녹지|관리|농림|자연환경보전)\s*지역")
_Q_DATE = re.compile(r"\d{4}\s*[년.\-/]\s*\d{1,2}\s*[월.\-/]\s*\d{1,2}\s*일?")
_Q_DAY = re.compile(r"(평일|주말|공휴일|휴일|[월화수목금토일]요일)")
_Q_UNIT_NUM = re.compile(r"\d[\d,\.]*\s*(?:천|만|억)?\s*(dB|㏈|데시벨|kg|㎏|킬로그램|톤|리터|ℓ|L(?![a-zA-Z])|㎡|제곱미터|㎥|세제곱미터|명|마리|두)")


def _unit_of(s: str) -> set:
    norm = {"㏈": "dB", "데시벨": "dB", "㎏": "kg", "킬로그램": "kg", "ℓ": "L", "리터": "L",
            "제곱미터": "㎡", "세제곱미터": "㎥"}
    return {norm.get(m.group(1), m.group(1)) for m in _Q_UNIT_NUM.finditer(s)}


def _known_in_question(question: str, item: dict) -> str:
    """되묻기 항목이 질문 원문에 이미 있는 사실을 묻는 것이면 그 이유를, 아니면 "" 를 돌려줍니다."""
    q = str(question or "")
    ask = str(item.get("question", ""))
    opts = " ".join(str(o) for o in item.get("options", []))
    if re.search(r"(시각|시간대|몇\s*시(?!간)|측정한\s*시간)", ask) and _Q_TIME_OF_DAY.search(q):
        return "측정 시각이 질문에 있음"
    if re.search(r"(사용\s*시간|작업\s*시간|가동\s*시간|몇\s*시간|사용하는\s*시간|1일\s*사용)", ask) \
            and _Q_DURATION.search(q):
        return "사용 시간이 질문에 있음"
    if "지역" in ask and _Q_ZONE.search(q) and _Q_ZONE.search(opts):
        return "대상 지역이 질문에 있음"
    if re.search(r"(공휴일|휴일|요일|평일)", ask) and _Q_DAY.search(q):
        return "요일이 질문에 있음"
    # "주거지역·아파트 공사장" 인데 "300m 안에 주택이 있습니까?" — 지시문에 예로 넣어도 계속 물어서 코드로 막습니다.
    if re.search(r"(주택|주거)", ask) and re.search(r"(주거\s*지역|아파트|주택가|주택\s*단지|빌라|다세대)", q):
        return "주거 시설이 질문에 있음"
    # 날짜: 질문에 "2020년 8월 20일에 … 검사" 처럼 그 일의 날짜가 이미 있으면 묻지 않습니다.
    if re.search(r"(언제|날짜|일자|연월일|시점)", ask):
        # 가장 구체적인 낱말 하나로 맞춥니다("완공검사일" 은 "완공" 으로 — "검사" 로 맞추면 엉뚱한 날짜에 걸림).
        kws = [k for k in ("완공", "착공", "준공", "취득", "등록", "허가", "신고", "설치", "사용", "검사")
               if k in ask]
        if kws:
            for m in _Q_DATE.finditer(q):
                around = q[max(0, m.start() - 12):m.end() + 16]
                if kws[0] in around:
                    return f"{kws[0]} 날짜가 질문에 있음"
    uq, uo = _unit_of(q), _unit_of(opts + " " + ask)
    if uq & uo:
        return f"수치({', '.join(sorted(uq & uo))})가 질문에 있음"
    return ""

# 되묻기가 "결론"(기한·의무)을 질문자에게 되묻는 꼴. clarify() 마지막 그물에서 버립니다.
#   "…언제까지 받아야 합니까?" / "…설치해야 합니까?" / "…신고해야 하나요?"
# 사실을 묻는 "언제 설치했습니까?" "용량은 얼마입니까?" 는 걸리지 않습니다.
# eval_run.py 에 같은 규칙이 복사돼 있습니다 (그쪽은 표준 라이브러리만 씀).
CONCLUSION_Q = re.compile(r"언제까지|야\s*(합니|하나요|하는지|할까요|됩니|되나요|하는가)"
                          # v1.32 — "보정 적용 여부는?", "기준일이 설치일로부터 계산됩니까?" 처럼
                          #   조문을 보고 이 도구가 판단할 것을 묻는 꼴
                          r"|(적용|보정|가산|감경|면제)\s*(여부|대상인|되나요|됩니까|되는지)"
                          r"|(계산|산정|기산)\s*(됩니까|되나요|하나요|합니까)")


# "…에 해당하나요?" 같은 예/아니오 질문인지 판별합니다.
# 예/아니오로 답하는 질문의 어미.
# ★ 2026-08-18 — "지정되었나요?" 처럼 `되었나요`·`했나요`·`인가?` 로 끝나는
#   물음이 빠져 있어서, 부정 보기가 없는 채로 화면에 나갔습니다
#   ("시설이 …로 지정되었나요?  ○ 지정됨  ○ 모름" — 아니라고 답할 방법이 없음).
_YESNO_RE = re.compile(
    r"(해당하나요|해당합니까|인가요|입니까|습니까|맞나요|있나요|없나요|하나요|"
    r"되나요|되었나요|됐나요|했나요|받았나요|였나요|이었나요|"
    r"인가|인지|되었는지|맞는가|있는가)\s*\??$")

# 의문사. 이게 있으면 예/아니오로 답하는 질문이 아닙니다.
# (어미는 같은데 답이 전혀 다릅니다 — "언제 받았나요?" 에 "아니오" 는 답이 아님)
_WH_RE = re.compile(
    r"(언제|어디|어느\s*곳|무엇|뭐|어떤|어떻게|어찌|얼마|몇|"
    r"누가|누구|왜|무슨|어느)")


# 보기로 흔히 쓰이는 낱말. 질문 자리에 이런 것이 오면 그 줄은 버립니다.
_OPTION_WORDS = {
    "예", "네", "아니오", "아니요", "아니다", "모름", "모르겠음",
    "해당함", "해당없음", "해당하지않음", "있음", "없음", "맞음", "아님",
    "미확인", "확인안됨", "yes", "no", "unknown",
}


def _looks_like_option(s: str) -> bool:
    t = re.sub(r"[\s?？.]", "", str(s or ""))
    return t in _OPTION_WORDS or len(t) < 2


# 출력 형식 틀의 라벨. "질문|보기1|보기2" 라는 틀을 로컬 모델이 그대로 베낍니다.
_LABEL_ONLY_RE = re.compile(
    r"^(질문|물음|보기\s*\d*|선택지\s*\d*|option\s*\d*|q|a)\s*$", re.I)
_LABEL_HEAD_RE = re.compile(
    r"^(질문|물음|보기\s*\d*|선택지\s*\d*|option\s*\d*|q|a)\s*[:：]\s*", re.I)


def _split_clarify_line(line: str) -> list[str]:
    """
    되묻기 한 줄을 [질문, 보기, 보기, …] 으로 쪼갭니다.

    기대 형식은  질문|보기1|보기2|보기3  입니다.
    그런데 로컬 모델(Qwen)이 형식 틀의 낱말을 그대로 베껴 이렇게 내놓습니다.

        질문|누출검사주기가 달라진 사유는?: 지하매설 저장시설 / 지상·옥내 저장시설 / 모름

    "|" 로만 자르면 조각이 2개("질문", 나머지 전부)뿐이라 3조각 조건에 걸려
    통째로 버려집니다. 실제로 멀쩡한 질문 3줄이 전부 사라졌습니다
    (로그: "[clarify] 0개 질문 파싱 (원문 3줄)").

    그래서 ① 라벨만 있는 조각을 버리고 ② 조각이 모자라면
    "질문: 보기 / 보기 / 모름" 형태를 한 번 더 풀어봅니다.
    """
    pieces = []
    for p in line.split("|"):
        p = p.strip().strip("\"'“”")
        if not p or _LABEL_ONLY_RE.match(p):
            continue                       # "질문" / "보기1" 같은 형식 라벨은 버립니다
        p = _LABEL_HEAD_RE.sub("", p).strip()
        if p:
            pieces.append(p)

    if len(pieces) >= 3:
        return pieces

    # 조각이 모자랍니다. 마지막 조각이 "질문?: 보기 / 보기 / 모름" 인지 봅니다.
    # ★ 구분자는 "/" 만 씁니다. "·" 로 자르면 "지상·옥내 저장시설" 이 쪼개집니다.
    tail = pieces[-1] if pieces else ""
    m = re.match(r"^(.{4,70}?)\s*[:：]\s*(.+)$", tail)
    if m:
        opts = _expand_slash_options([m.group(2)])
        if len(opts) >= 2:
            q = pieces[0] if len(pieces) >= 2 else m.group(1).strip()
            return [q] + opts

    # ★ 2026-08-19 — 콜론 없이 보기를 한 칸에 "/" 로만 몰아넣는 형태.
    #     저장시설 종류|지하매설/지상·옥내/모름
    #   조각이 2개라 3조각 조건에 걸려 **줄째로 버려지고 있었습니다.**
    if len(pieces) == 2:
        opts = _expand_slash_options([pieces[1]])
        if len(opts) >= 2:
            return [pieces[0]] + opts
    return pieces


# 확실한 물음 어미. 보기 자리에 오면 안 됩니다.
_OPT_LOOKS_LIKE_Q_RE = re.compile(r"(인가요|입니까|습니까|나요|은가요)\s*\??$")

# ★ 2026-08-18 — 물음표가 **붙은** 어미. 위 목록에 "인가/인지/는가" 를 그냥
#   넣으면 "이중벽인지", "2만리터 이상인지" 같은 멀쩡한 보기까지 버려집니다
#   (예전에 실제로 그랬습니다). 물음표를 요구하면 그 사고 없이
#   "지상·옥내 저장시설인가?" 만 정확히 걸러집니다.
_OPT_Q_MARK_RE = re.compile(r"(인가|인지|는가|ㄴ가|맞나|맞는가)\s*\?$")

# 물음 어미를 떼어내 보기 문구로 만들 때 씁니다.
_Q_TAIL_RE = re.compile(
    r"\s*(인가요|입니까|습니까|인가|인지|나요|은가요|는가|맞나요|맞나|맞는가)\s*\?*$")

# "A인지 B인지" 형태의 질문. 여기서 보기를 뽑아낼 수 있습니다.
_A_OR_B_RE = re.compile(r"^(.{2,25}?)\s*인지\s+(.{2,25}?)\s*인지\s*\??$")


def _opt_is_question(o: str) -> bool:
    """
    보기 자리에 온 것이 사실은 질문인지 판정합니다.

    ★ "인지" 로 끝난다는 것만으로 버리면 안 됩니다.
      "이중벽인지", "2만리터 이상인지" 처럼 보기로 충분히 쓸 만한 말까지
      함께 버려집니다. 실제로 저장시설 용량·이중벽을 묻던 질문이 통째로
      사라졌습니다.

    두 갈래를 한 칸에 담고 있거나("석유류인지 유해화학물질인지"),
    확실한 물음 어미로 끝날 때만 질문으로 봅니다.
    """
    o = (o or "").strip()
    if not o:
        return False
    return bool(_OPT_LOOKS_LIKE_Q_RE.search(o)
                or _OPT_Q_MARK_RE.search(o)
                or _A_OR_B_RE.match(o))


# "…확인이 필요합니다" 처럼 **할 일**로 쓴 꼬리말.
# 프롬프트에 "할 일을 보기로 만들지 마라" 고 적어 뒀지만 로컬 모델이 지키지
# 않습니다. 코드에서 걸러야 합니다.
_TODO_TAIL_RE = re.compile(
    r"\s*(에 대한|에 대해|의)?\s*"
    r"(확인|검토|파악|조회|점검)\s*(이|가|을|를)?\s*"
    r"(필요합니다|필요함|필요|필요합니까|필요한가요|필요한지|"
    r"요망합니다|요망|해야 합니다|해야함|해야 하나요|해야 합니까|"
    r"바랍니다)\s*[\.\?]?$")

# "…궁금합니다 / …알고 싶습니다" — 질문이 아니라 하소연입니다.
_WISH_TAIL_RE = re.compile(
    r"\s*(이|가)?\s*(궁금합니다|궁금함|알고 싶습니다|알고싶습니다|"
    r"알아야 합니다|모르겠습니다)\s*\.?$")

# "A인지 B인지" — 앞의 군말 길이를 제한하지 않는 느슨한 판. 보기를 뽑는 데 씁니다.
_A_OR_B_LOOSE_RE = re.compile(r"^(.+?)\s*인지\s+(.{1,30}?)\s*인지\s*[\.\?]?$")


def _strip_todo(s: str) -> str:
    """보기 끝의 '…확인이 필요합니다' 류 군말을 떼어냅니다."""
    t = str(s or "").strip()
    prev = None
    while t != prev:                      # "확인이 필요합니다" + "." 처럼 겹칠 수 있음
        prev = t
        t = _TODO_TAIL_RE.sub("", t).strip()
        t = _WISH_TAIL_RE.sub("", t).strip()
    return t.rstrip(" .·,")


# "모름" 과 같은 뜻으로 쓰이는 보기. 이건 버리면 안 됩니다.
_UNKNOWN_OPTS = ("모름", "모르겠음", "모르겠습니다", "모르겠어요", "잘모름",
                 "확인안됨", "미확인", "확인필요없음")


def _is_unknown_option(s: str) -> bool:
    return str(s or "").replace(" ", "").rstrip(".") in _UNKNOWN_OPTS


def _is_todo_option(s: str) -> bool:
    """보기 자리에 온 것이 '할 일' 문장인지."""
    s = str(s or "").strip()
    if not s:
        return False
    # ★ 2026-08-19 — "모르겠습니다" 는 _WISH_TAIL_RE 에 걸려 '할 일'로 판정돼
    #   **삭제되고 있었습니다.** 그러면 화면에 "모름" 계열 보기가 하나도 안 남고,
    #   index.html 은 그럴 때 **마지막 보기를 기본 선택**하므로, 사용자가 그냥
    #   "이대로 검색" 을 누르면 모르는 사실을 확정 조건으로 넣게 됩니다.
    if _is_unknown_option(s):
        return False
    return bool(_TODO_TAIL_RE.search(s) or _WISH_TAIL_RE.search(s))


def _trim_parallel(a: str, b: str) -> str:
    """
    "소유주명의로 등록한지 10년 미만" / "10년 이상" 처럼 앞쪽에만 군말이 붙은
    짝을 맞춰 줍니다. 긴 쪽에서 짧은 쪽과 같은 어절 수만 남깁니다.
      → "10년 미만" / "10년 이상"

    ★ 2026-08-19 — 어절 수만 보고 자르면 **구분하는 말 자체를 잘라먹습니다.**
      실측: ("해당 시설이 지하에 매설된 저장시설", "지상 저장시설")
            → "매설된 저장시설" / "지상 저장시설"  ← '지하' 가 사라져 대비가 무너짐
      그래서 남는 쪽이 **혼자서도 뜻이 서는 형태**일 때만 자릅니다.
      실무에서 자를 값어치가 있는 건 대부분 수치라, 숫자로 시작하거나
      비교어(이상/미만/초과/이하)를 포함할 때만 자릅니다.
    """
    wa, wb = a.split(), b.split()
    if not (len(wa) > len(wb) >= 1):
        return a
    cut = " ".join(wa[-len(wb):])
    if re.match(r"^[\d,\.]", cut) or re.search(r"(이상|미만|초과|이하)", cut):
        return cut
    return a


def _promote_todo_options(question: str, options: list) -> list:
    """
    보기 자리에 '할 일' 문장이 여러 개 들어온 줄을 **여러 개의 질문**으로 폅니다.

    실제 사례 (2026-08-18 로그):
        질문|누출검사 주기가 달라지는 시설 규모나 용량 기준이 궁금합니다.
            |소유주명의로 등록한지 10년 미만인지 10년 이상인지 확인이 필요합니다.
            |시설 내 저장탱크의 총 용량이 30m³ 미만인지 30m³ 이상인지 확인이 필요합니다.
            |저장된 물질이 석유류인지 유해화학물질인지 확인이 필요합니다.

    질문 자리에는 하소연("…궁금합니다")이, 보기 자리에는 할 일 세 개가
    들어왔습니다. 화면은 이렇게 됩니다 — 무엇을 고르라는 건지 알 수 없습니다.

        누출검사 주기가 달라지는 시설 규모나 용량 기준이 궁금합니다.
          ○ …확인이 필요합니다.  ○ …확인이 필요합니다.  ○ …확인이 필요합니다.

    그런데 보기 하나하나가 **그 자체로 멀쩡한 질문**입니다("A인지 B인지").
    군말을 떼고 각각을 질문으로 세우면 원래 물으려던 것이 살아납니다.

        소유주명의로 등록한지 10년 미만인지 10년 이상인지
          ○ 10년 미만  ○ 10년 이상  ○ 모름
        시설 내 저장탱크의 총 용량이 30m³ 미만인지 30m³ 이상인지
          ○ 30m³ 미만  ○ 30m³ 이상  ○ 모름
        저장된 물질이 석유류인지 유해화학물질인지
          ○ 석유류  ○ 유해화학물질  ○ 모름

    조건이 아니면 None. (보기에서 A·B 를 못 뽑으면 그 줄은 버립니다 —
    "…확인이 필요합니다" 를 그대로 보기로 내보내느니 안 묻는 편이 낫습니다.)
    """
    UNKNOWN = ("모름", "모르겠음", "확인안됨", "미확인")
    todos = [o for o in options
             if o and o.replace(" ", "") not in UNKNOWN and _is_todo_option(o)]
    if len(todos) < 2:
        return None

    out = []
    for o in todos:
        body = _strip_todo(o)
        m = _A_OR_B_LOOSE_RE.match(body)
        if not m:
            continue
        a, b = m.group(1).strip(), m.group(2).strip()
        a = _trim_parallel(a, b)
        if not a or not b or a == b:
            continue
        out.append({"question": body[:60], "options": [a, b, "모름"]})
    return out or None


def _clean_question(q: str, options: list) -> str:
    """
    질문 문구를 다듬습니다.

    ★ 2026-08-19 실사용 사고 —
      2차 되묻기에서 화면이 이렇게 나왔습니다.

          지금까지: 누출검사 대상인지 확인이 필요합니다.: 지하매설 저장시설

          누출검사 대상인지 확인이 필요합니다.: 지하매설 저장시설
            ○ 지상·옥내 저장시설   ● 모름

      질문 자리에 **1차에서 고른 답이 통째로 붙어** 있습니다. 모델이
      [이미 답변된 조건] 을 그대로 베껴 새 질문으로 낸 것입니다.
      질문·보기가 어긋나서 무엇을 고르라는 건지 알 수 없습니다.

      두 가지를 처리합니다.
        (1) "질문: 답" 꼴로 답이 새어 들어왔으면 콜론 뒤를 잘라냅니다.
            (뒤쪽이 보기 중 하나이거나, 짧은 답변처럼 보일 때만)
        (2) "…확인이 필요합니다" 같은 할 일 꼬리말을 뗍니다.
            질문 자리에 있어도 읽기 나쁩니다 — "누출검사 대상인지" 로 충분합니다.
    """
    q = str(q or "").strip()
    if not q:
        return q

    # (1) 새어 들어온 답 잘라내기
    for sep in (":", "："):
        if sep in q:
            head, tail = q.split(sep, 1)
            head, tail = head.strip(), tail.strip()
            if not head:
                continue
            leaked = (tail in [str(o).strip() for o in options]
                      or _is_unknown_option(tail)
                      or (len(tail) <= 25 and not tail.endswith("?")))
            if leaked:
                q = head
                break

    # (2) 할 일·하소연 꼬리말 떼기
    cleaned = _strip_todo(q)
    if len(cleaned) >= 4:                 # 너무 짧아지면 원문을 둡니다
        q = cleaned
    return q.strip()


def _common_tail(a: str, b: str) -> str:
    """두 문구의 공통 꼬리말(어절 단위)을 돌려줍니다. 없으면 ""."""
    wa, wb = a.split(), b.split()
    tail = []
    while wa and wb and wa[-1] == wb[-1]:
        tail.insert(0, wa.pop())
        wb.pop()
    return " ".join(tail)


def _repair_all_questions(question: str, options: list) -> list:
    """
    질문과 보기가 **둘 다 물음** 인 줄을 고칩니다.

    실제 사례 (2026-08-18 로그):
        질문|주유소 지하매설 저장시설인가?|지상·옥내 저장시설인가?|모름

    모델이 서로 배타적인 두 상태를 각각 물음으로 써 놓은 것입니다.
    화면은 이렇게 됩니다 — 첫 번째를 고를 방법이 없습니다.
        주유소 지하매설 저장시설인가?
          ○ 지상·옥내 저장시설인가?   ○ 모름

    → 물음 어미를 떼어 **양쪽 다 보기로** 세우고, 공통 꼬리말로 질문을
      새로 만듭니다.
        저장시설 중 어느 쪽입니까?
          ○ 주유소 지하매설 저장시설   ○ 지상·옥내 저장시설   ○ 모름

    고칠 조건이 아니면 (question, options) 를 그대로 돌려줍니다.
    """
    q = (question or "").strip()
    q_is_question = bool(_OPT_LOOKS_LIKE_Q_RE.search(q) or _OPT_Q_MARK_RE.search(q))
    if not q_is_question:
        return None

    others = [o.strip() for o in options
              if o and not _is_unknown_option(o)]
    # ★ 2026-08-19 — 물음 어미만 떼면 "면제 대상인지 확인해야 하나요?" 가
    #   "면제 대상인지 확인해야 하" 라는 **잘린 조각**으로 남습니다.
    #   할 일 문장은 여기서 미리 걸러냅니다.
    others = [o for o in others if not _is_todo_option(o)]
    if not others or not any(_opt_is_question(o) for o in others):
        return None

    a = _Q_TAIL_RE.sub("", q).strip()
    picks = [a]
    for o in others:
        b = _Q_TAIL_RE.sub("", o).strip()
        if b and b not in picks:
            picks.append(b)
    if len(picks) < 2:
        return None

    tail = _common_tail(picks[0], picks[1])
    new_q = f"{tail} 중 어느 쪽입니까?" if tail else "다음 중 어느 쪽입니까?"
    return {"question": new_q, "options": picks[:4] + ["모름"]}


def _expand_slash_options(options: list) -> list:
    """
    한 칸에 "/" 로 몰아넣은 보기를 풀어냅니다.

    실제 사례 — 모델이 보기 셋을 한 칸에 넣고 "모름" 만 따로 붙였습니다.
        지하 저장시설의 용량 범위|5,000 리터 미만 / 5,000 리터 이상 / 모름|모름

    조각이 3개라 형식 검사(질문+보기2)는 통과하지만, 화면은 이렇게 됩니다.
        지하 저장시설의 용량 범위
          ● 5,000 리터 미만 / 5,000 리터 이상 / 모름     ○ 모름
    고를 수 있는 것이 사실상 없습니다.

    → "/" 로 갈라 펴고, 중복(위 예의 "모름" 두 번)을 없앱니다.
        ○ 5,000 리터 미만   ○ 5,000 리터 이상   ○ 모름

    ★ 구분자는 "/" 만 씁니다. "·" 로 자르면 "지상·옥내 저장시설" 이 쪼개지고
      "토양오염도검사·누출검사" 같은 법령 용어도 망가집니다.

    ★ 2026-08-19 — 그런데 "/" 도 그냥 자르면 안 됩니다. 환경 분야 보기는
      **단위에 "/" 가 들어갑니다.** 실측:
          700㎥/일 미만|700㎥/일 이상|모름
          → ['700㎥', '일 미만', '일 이상', '모름']     ← 산산조각
      사용자가 "일 미만" 을 고르면 그게 확인된 조건으로 답변에 들어갑니다.
      그래서 **양옆 중 한쪽에라도 공백이 있는 "/" 만** 구분자로 봅니다
      ("700㎥/일" 은 붙어 있으므로 안전, "A 미만 / A 이상" 은 갈라집니다).
      공백 없이 "A/B/모름" 으로 낸 경우는, 단위가 안 섞였을 때만 한 번 더
      갈라 줍니다.
    """
    # 숫자+단위 뒤에 바로 "/" 가 붙는 형태 (㎥/일, mg/L, 톤/년 …)
    unit_slash = re.compile(r"\d\s*[a-zA-Z가-힣㎥㎡㎎㎍ℓ%]*\s*/")

    def _split(s: str) -> list:
        parts = [p for p in re.split(r"\s+/\s*|\s*/\s+", s) if p.strip()]
        if len(parts) == 1 and "/" in s and not unit_slash.search(s):
            parts = [p for p in s.split("/") if p.strip()]
        return parts

    out = []
    for o in options:
        for part in _split(str(o or "")):
            part = part.strip().strip("\"'“”")
            if part and part not in out:
                out.append(part)
    return out


def _repair_a_or_b(question: str, options: list) -> list:
    """
    보기 자리에 질문이 들어온 줄을 고칩니다.

    로컬 모델이 질문 여러 개를 한 줄에 몰아넣는 일이 잦습니다.
        지상·옥내 저장시설인지 지하 저장시설인지|석유류인지 유해화학물질인지|…|모름

    그러면 화면이 이렇게 됩니다. 고를 수가 없습니다.
        지상·옥내 저장시설인지 지하 저장시설인지
          ○ 석유류인지 유해화학물질인지  ○ 15년 미만인지 15년 이상인지  ● 모름

    보기 자리의 질문은 버리고, 질문 자체가 "A인지 B인지" 형태면
    거기서 A·B 를 뽑아 보기로 세웁니다. 그러면 원래 물으려던 것이 살아납니다.
        지상·옥내 저장시설인지 지하 저장시설인지
          ○ 지상·옥내 저장시설  ○ 지하 저장시설  ○ 모름
    """
    real = [o for o in options if o and not _opt_is_question(o)]
    if len(real) >= 2:
        return real
    m = _A_OR_B_RE.match((question or "").strip())
    if m:
        a, b = m.group(1).strip(), m.group(2).strip()
        if a and b and a != b:
            return [a, b, "모름"]
    return real


def _fix_yesno(question: str, options: list) -> list:
    """
    예/아니오 질문인데 한쪽 보기만 있으면 반대쪽을 채웁니다.

    실제 사례: "배출하는 폐기물이 방사성폐기물에 해당하나요?" 의 보기가
    "방사성폐기물" 하나뿐이라 "아니다" 를 고를 수 없었습니다.
    부정 답변은 검색 범위를 좁히는 중요한 조건이므로 반드시 있어야 합니다.

    단, "지하매설 / 지상·옥내" 처럼 이미 서로 대립하는 실질 선택지가
    둘 이상이면 그대로 둡니다. 그쪽이 정보량이 더 많습니다.
    """
    opts = [o for o in options if o]
    q = question.strip()
    if not _YESNO_RE.search(q):
        return opts

    # ★ 2026-08-19 — 어미만 보면 **의문사 질문**까지 걸립니다.
    #   "마지막 누출검사를 언제 받았나요?" 도 `받았나요?` 로 끝나거든요.
    #   그러면 보기에 "아니오" 가 붙어서
    #       마지막 누출검사를 언제 받았나요?  ○ 아래 칸에 날짜 입력  ○ 아니오
    #   가 됩니다. 사용자가 "아니오" 를 고르면
    #   "마지막 누출검사를 언제 받았나요?: 아니오" 가 확인된 조건으로 답변
    #   프롬프트에 들어가고, 거기서 다음 검사일을 계산하려 듭니다.
    #   하필 이게 우리가 **최우선으로 묻게 만든** 질문이라 더 나쁩니다.
    if _WH_RE.search(q):
        return opts

    # ★ 2026-09-29 — 실질 보기가 **부정 하나뿐**인 경우.
    #   실사례: "악취배출시설이 있습니까?" → [악취배출시설이 없습니다 / 모름]
    #   "있다" 를 고를 수 없었습니다. 부정 문장을 긍정으로 바꿔 앞에 넣습니다.
    _UNK = ("모름", "모르겠음", "모르겠어요", "잘모름", "확인안됨", "미확인")
    real0 = [o for o in opts if o.replace(" ", "") not in _UNK]
    if len(real0) == 1:
        neg = real0[0].strip()
        for tail, pos in (("해당하지 않습니다", "해당합니다"), ("해당하지 않음", "해당함"),
                          ("없습니다", "있습니다"), ("없음", "있음"), ("아닙니다", "맞습니다"),
                          ("아님", "맞음"), ("않습니다", "합니다"), ("않음", "함")):
            if neg.endswith(tail) and neg != tail:
                return [neg[: -len(tail)] + pos, neg, "모름"]

    NO_WORDS = ("아니오", "아니요", "아니다", "해당없음", "해당하지않음",
                "없음", "아님", "미해당", "비해당")
    UNKNOWN = ("모름", "모르겠음", "모르겠어요", "잘모름",
               "확인안됨", "미확인")

    flat = [o.replace(" ", "") for o in opts]
    has_no = any(o in NO_WORDS for o in flat)
    if has_no:
        return opts                      # 부정 보기가 이미 있으면 그대로

    # "모름" 을 뺀 실질 보기가 둘 이상이면 대립 선택지로 보고 유지합니다.
    real = [o for o, f in zip(opts, flat) if f not in UNKNOWN]
    if len(real) >= 2:
        return opts

    # ★ 2026-08-18 — 실질 보기가 하나뿐일 때 예전에는 통째로
    #   ["예", "아니오", "모름"] 으로 **갈아치웠습니다.** 그래서
    #       저장된 물질이 석유류인가요?  ○ 석유류  ○ 모름
    #   이 이렇게 바뀌었습니다.
    #       저장된 물질이 석유류인가요?  ○ 예  ○ 아니오  ○ 모름
    #   모델이 애써 뽑아 준 "석유류" 라는 구체적인 말이 사라지고, 담당자는
    #   질문을 다시 읽어야 무엇에 "예" 인지 알 수 있습니다. 정보가 줄어듭니다.
    #   (실사용 지적: "두 번째 되묻기에 예 라고 글자가 잘못 들어간다")
    #
    #   → 있는 보기는 그대로 두고 **부정만 채웁니다.**
    #       저장된 물질이 석유류인가요?  ○ 석유류  ○ 아니오  ○ 모름
    if real:
        out = []
        for o in opts:
            if o.replace(" ", "") in UNKNOWN:
                continue
            out.append(o)
        out.append("아니오")
        out.append("모름")
        return out

    # 실질 보기가 아예 없을 때만 예/아니오로 세웁니다.
    return ["예", "아니오", "모름"]


def _finalize_clarify(items: list, question: str = "") -> list:
    """되묻기 항목의 마지막 그물 — 텍스트·JSON 두 경로가 함께 씁니다 (v1.31 에 함수로 분리).
    할 일 보기·너무 짧은 보기 제거, "모름" 정리, 물음 아닌 질문·법령명 질문·결론 질문 제거."""
    cleaned = []
    seen_q = set()
    for item in items:
        # v1.34 — 한 라운드에 같은 질문이 두 번 나오는 경우(실측)
        qk = re.sub(r"[\s?？.]+", "", str(item.get("question", "")))
        if qk in seen_q:
            continue
        seen_q.add(qk)
        left = [o for o in item["options"] if not _is_todo_option(o)]
        if len(left) < len(item["options"]) and LLM_DEBUG:
            _dbg(f"[clarify] '할 일' 보기 {len(item['options']) - len(left)}개 제거: "
                  f"{item['question'][:30]}")
        # 잘린 조각이 남지 않게 너무 짧은 보기도 버립니다.
        # 단 "예"/"네" 는 한 글자여도 정상 보기입니다.
        left = [o for o in left
                if len(o.strip()) >= 2 or o.strip() in ("예", "네")]
        # ★ 2026-09-29 — "모름" 계열 보기가 둘 이상이면("모르겠음 / 모름") 하나만 남겨 맨 뒤로.
        unk = [o for o in left if _is_unknown_option(o)]
        if unk:
            left = [o for o in left if not _is_unknown_option(o)] + ["모름"]
        # ★ 2026-09-29 — 물음이 아닌 질문("우려기준 초과")은 버립니다.
        #   사용자가 무엇을 골라야 하는지 알 수 없습니다.
        if not re.search(r"(\?|？|까|요|나|가|지|니|죠)\s*$", item["question"].strip()):
            _dbg(f"[clarify] 물음이 아닌 질문 제거: {item['question'][:40]}")
            continue
        # ★ 2026-09-29 — "어느 법이 적용되냐" 를 묻는 항목은 버립니다.
        #   질문자는 법을 모르고, 법을 가리는 건 이 도구의 일입니다.
        #   실제 사례: 질문 "토양환경보전법" / 보기 "광산피해의 … 법률 | 모름"
        q_bare = re.sub(r"[\s?？.]+$", "", item["question"])
        law_opts = [o for o in left if _looks_like_law(o.strip())]
        if _looks_like_law(q_bare) or law_opts:
            if LLM_DEBUG:
                _dbg(f"[clarify] 법령 이름을 묻는 항목 제거: {item['question'][:30]} "
                      f"/ {', '.join(left)[:60]}")
            continue
        # ★ 2026-10-02 — 결론(기한·의무)을 질문자에게 되묻는 항목은 버립니다.
        #   실제 사례: 누출검사 주기를 물었는데 되묻기가
        #   "다음 정기 누출검사를 언제까지 받아야 합니까?" — 질문자가 물은 것 그 자체.
        if CONCLUSION_Q.search(item["question"]):
            _dbg(f"[clarify] 결론을 되묻는 항목 제거: {item['question'][:40]}")
            continue
        # ★ v1.34 — 보기 손질 (2026-10-08 실사용 피드백: "환경영향평가를 받아야 하나요?")
        #   · "별표 3 기준 이상/미만" 처럼 조문·별표를 가리키는 보기 → 질문자는 그 기준을 모릅니다.
        #   · "규모는 얼마입니까?" 인데 보기에 숫자가 없음 → 질문과 보기가 안 맞습니다.
        #   → 수치를 묻는 질문이면 직접 입력으로 바꾸고, 아니면 그 질문을 버립니다.
        q_txt = item["question"]
        asks_qty = bool(re.search(r"(얼마|몇\s*\S{0,3}|규모|면적|용량|수량|크기|배출량|인원|금액|길이|높이)", q_txt))
        refs_law = [o for o in left if re.search(r"(별표|별지|제\s*\d+\s*조|시행령|시행규칙)", o)
                    or (re.search(r"기준\s*(이상|미만|초과|이하)", o) and not re.search(r"\d", o))]
        no_digit = asks_qty and not any(re.search(r"\d", o) for o in left if not _is_unknown_option(o)) \
            and not any("입력" in o for o in left)
        if refs_law or no_digit:
            if asks_qty:
                left = ["아래 칸에 직접 입력", "모름"]
            else:
                _dbg(f"[clarify] 조문·별표를 가리키는 보기 → 제거: {q_txt[:40]}")
                continue
        # 보기가 범주 목록이면(예/아니오·수치 범위가 아니면) "기타" 를, 그리고 항상 "모름" 을 둡니다.
        #   (실사용: "사업의 종류" 보기에 맞는 것이 없어 아무거나 고른 뒤 답이 엉뚱해짐)
        real = [o for o in left if not _is_unknown_option(o)]
        categorical = len(real) >= 2 and not any(
            re.search(r"(^예$|^아니오|^네$|있음|없음|이상|미만|초과|이하|\d|입력|받은 적|해당)", o) for o in real)
        if categorical and not any(re.search(r"(기타|그 밖|그밖|직접)", o) for o in real):
            real.append("기타 (아래 칸에 적기)")
        left = real + ["모름"]
        item = {"question": q_txt, "options": left}
        # ★ v1.32 — 질문 원문에 이미 적힌 사실(시각·사용 시간·지역·요일·수치)을 되묻는 항목은 버립니다.
        why = _known_in_question(question, {"question": item["question"], "options": left})
        if why:
            _dbg(f"[clarify] 이미 질문에 있는 사실 → 제거 ({why}): {item['question'][:40]}")
            continue
        if len(left) >= 2 and re.search(r"[가-힣]", item["question"]):
            cleaned.append({"question": item["question"], "options": left})
    return cleaned


def clarify(question: str, answered: str = "", catalog: str = "") -> list[dict]:
    """
    질문이 애매하면 선택지를 돌려줍니다. 더 물을 게 없으면 빈 목록.

    catalog 에는 이 질문에 쓰려고 고른 조문·별표의 **본문 발췌**를 넘깁니다 (v1.31).
    v1.30 까지는 조문 **제목** 목록 앞 60줄만 넘겨서, 기준값(2만L·1년·3회 등)을
    모른 채 제목 단어를 되묻거나("…에 해당하나요?") 보기를 못 나눴습니다.
    이것이 없으면 AI 가 기억에 의존해 존재하지 않는 용어를 지어냅니다.
    (실제 사례: "특정토양오염유발시설" — 법령에 없는 이름)
    """
    fill = dict(question=question, answered=answered or "(없음)",
                catalog=catalog or "(아직 수집된 조문이 없습니다. 일반적인 표현으로만 물으세요.)")

    # ★ v1.31 — JSON 형식 강제를 먼저 시도합니다. 성공하면 형식 보정 단계를
    #   건너뛰고 마지막 그물(_finalize_clarify)만 통과시킵니다.
    obj, _ = call_json(CLARIFY_PROMPT.format(output_rules=_CLARIFY_RULES_JSON, **fill),
                       CLARIFY_SCHEMA, temperature=0.2,
                       max_tokens=MAXTOK_CLARIFY, stage="clarify")
    if isinstance(obj, dict):
        items = []
        if not obj.get("done") or obj.get("questions"):
            for it in (obj.get("questions") or [])[:3]:
                if not isinstance(it, dict):
                    continue
                q = _clean_question(str(it.get("question") or "").strip()[:80],
                                    [str(o) for o in (it.get("options") or [])])
                opts = [str(o).strip()[:30] for o in (it.get("options") or []) if str(o).strip()]
                opts = _expand_slash_options(opts)
                opts = _fix_yesno(q, opts)
                if len(q) >= 4 and len(opts) >= 2:
                    items.append({"question": q, "options": opts})
        out = _finalize_clarify(items, question)
        if LLM_DEBUG:
            _dbg(f"[clarify] JSON {len(items)}개 → 최종 {len(out)}개")
        return out[:6]

    raw = _call(CLARIFY_PROMPT.format(output_rules=_CLARIFY_RULES_TEXT, **fill),
                max_tokens=MAXTOK_CLARIFY, stage="clarify").strip()

    # "OK" 만 나오면 더 물을 게 없다는 뜻입니다.
    # 로컬 모델은 "OK." "OK 입니다" 처럼 덧붙이기도 합니다.
    head = raw.strip().splitlines()[0].strip() if raw.strip() else ""
    if re.match(r"^ok\b", head, re.I) or "|" not in raw:
        if LLM_DEBUG:
            _dbg(f"[clarify] 더 물을 것 없음 (응답: {head[:60]})")
        return []

    out = []
    for line in raw.splitlines():
        line = line.strip().strip("-*· ")
        line = re.sub(r"^\d+[.)]\s*", "", line)
        if "|" not in line:
            continue
        # ★ 2026-08-19 — 모델이 마크다운 표로 답할 때가 있습니다.
        #   정렬 행("|:---|:---:|---:|")이 그대로 질문·보기로 올라가
        #   화면에 ":---" 가 보기로 뜹니다. 한글이 한 글자도 없으면 버립니다.
        if not re.search(r"[가-힣]", line):
            continue
        # 모델이 형식 틀("질문|보기1|…")의 낱말을 그대로 베끼거나
        # "질문: 보기 / 보기 / 모름" 으로 흘러도 읽어냅니다.
        parts = _split_clarify_line(line)
        # ★ 최소 3조각(질문 + 보기 2개)이어야 합니다.
        #   2개로 완화하면 모델이 질문 없이 "예|아니오|모름" 만 뱉은 줄까지
        #   받아들여 "예" 가 질문이 되어버립니다.
        if len(parts) < 3:
            continue
        q, opts = parts[0][:60], parts[1:5]

        # 첫 조각이 보기 같은 낱말이면 질문이 아닙니다. 그 줄은 버립니다.
        if _looks_like_option(q):
            continue

        # ★ 질문에 새어 들어온 이전 답변·할 일 꼬리말을 먼저 걷어냅니다.
        #   이걸 안 하면 아래 보정들이 모두 오염된 질문을 기준으로 돕니다.
        q2 = _clean_question(q, opts)
        if q2 != q and LLM_DEBUG:
            _dbg(f"[clarify] 질문 정리: {q}  →  {q2}")
        q = q2
        if len(q) < 4:
            continue

        # 한 칸에 "/" 로 몰아넣은 보기를 먼저 폅니다.
        opts = _expand_slash_options(opts)

        # ★ 보기 자리에 "…확인이 필요합니다" 같은 할 일이 여러 개 들어온 줄은
        #   여러 개의 질문으로 폅니다. 제일 먼저 봅니다 — 이 형태는 아래
        #   보정들이 전부 그냥 통과시켜 버립니다(물음 어미가 없어서).
        todo_split = _promote_todo_options(q, opts)
        if todo_split:
            if LLM_DEBUG:
                _dbg(f"[clarify] 보기가 '할 일' 문장 → 질문 {len(todo_split)}개로 폄")
            out.extend(todo_split)
            continue

        # ★ 질문과 보기가 둘 다 물음인 줄("A인가?|B인가?|모름")을 먼저 봅니다.
        #   _repair_a_or_b 보다 앞에 둡니다 — 그쪽은 보기의 질문을 **버리는**
        #   방향이라, 여기서 살릴 수 있는 선택지가 먼저 사라집니다.
        fixed = _repair_all_questions(q, opts)
        if fixed:
            if LLM_DEBUG:
                _dbg(f"[clarify] 질문·보기가 모두 물음 → 보기로 재구성: "
                      f"{fixed['question']} / {', '.join(fixed['options'])}")
            out.append(fixed)
            continue

        # 보기 자리에 질문이 들어온 줄을 고칩니다.
        opts = _repair_a_or_b(q, opts)
        opts = _fix_yesno(q, opts)

        if len(opts) >= 2:
            out.append({"question": q, "options": opts})

    # ★ 마지막 그물 — 여기까지 와서도 "…확인이 필요합니다" 가 남아 있으면
    #   화면에 내보내지 않습니다. 고를 수 없는 보기를 보여 주느니
    #   그 질문을 안 묻는 편이 낫습니다.
    #   ★ 2026-08-19 — 예전에는 이 그물이 마지막 분기 안에만 있어서,
    #     `continue` 로 빠져나가는 _promote_todo_options / _repair_all_questions
    #     결과는 **검사를 통째로 건너뛰었습니다.** 루프 밖으로 뺐습니다.
    out = _finalize_clarify(out, question)

    if LLM_DEBUG:
        _dbg(f"[clarify] {len(out)}개 질문 파싱 (원문 {len(raw.splitlines())}줄)")
    return out[:6]


SELECT_PROMPT = """너는 대한민국 법령 조문을 골라내는 도구다.

아래 [조문 목록] 은 법령명과 조문 번호·제목만 나열한 것이다.
[질문] 에 답하려면 어떤 조문의 본문을 읽어야 하는지 고르라.

{output_rules}
**질문에 답하는 데 가장 중요한 조문부터** 순서대로 적는다.
(분량이 넘치면 뒤에 적은 것부터 뺀다)

가장 중요한 규칙 — 조문 제목을 반드시 읽어라:
- **제목이 질문과 무관하면 절대 고르지 마라.** 번호가 비슷하다고 고르면 안 된다.
  예) 질문이 "검사주기" 인데 제목이 "타인 토지에의 출입 등" 이면 → 고르지 않는다.
      질문이 "검사주기" 인데 제목이 "신고 등" 이면 → 고르지 않는다.
- 법률·시행령·시행규칙에는 **같은 번호의 조문이 각각 따로 있다.**
  법률 제8조와 시행령 제8조는 전혀 다른 내용이다.
  번호가 아니라 **[대괄호 안의 법령명 + 제목]** 을 보고 판단하라.
- **별표·별지는 적극적으로 고르라.** 기준·주기·금액 같은 구체적 수치는
  조문 본문이 아니라 별표에 적혀 있는 경우가 많다.
  질문이 수치를 묻는다면 관련 별표를 반드시 포함하라.
- 기준·기한·주기·금액 같은 구체적 수치는 대개 시행령·시행규칙에 있다.
  법률에는 근거만 있는 경우가 많으므로, 수치를 묻는 질문이면
  시행령·시행규칙의 해당 조문을 우선 고르라.

그 밖에:
- 절차를 묻는 질문이면 그 절차의 앞뒤 단계 조문도 함께 골라라.
- 용어의 뜻이 답에 꼭 필요할 때만 정의 조문을 고른다.
- 목적·적용제외·타인 토지 출입·권한 위임 같은 총칙·부수 조문은
  질문이 직접 그것을 묻는 경우에만 고른다.
- 5개에서 20개 사이로 고른다. 관련 조문이 적으면 적게 골라도 된다.
  많이 고르는 것보다 **정확히 고르는 것**이 중요하다.

[질문]
{question}

[조문 목록]
{catalog}
"""


_SELECT_RULES_TEXT = """출력 형식 — 고른 번호만 쉼표로 나열한다. 다른 말은 쓰지 않는다.
예) 25,7,3,12"""

_SELECT_RULES_JSON = """출력 형식 — JSON 하나만 출력한다.  예) {"ids": [25, 7, 3, 12]}"""

SELECT_SCHEMA = {
    "type": "object",
    "properties": {"ids": {"type": "array", "items": {"type": "integer"},
                           "minItems": 1, "maxItems": 40}},
    "required": ["ids"],
}


def select_prompt(question: str, catalog: str) -> str:
    """선별 프롬프트 (토큰 계산용으로 main.py 도 씁니다). 지금 쓰는 출력 형식을 따릅니다."""
    rules = _SELECT_RULES_JSON if json_enabled() else _SELECT_RULES_TEXT
    return SELECT_PROMPT.format(question=question, catalog=catalog, output_rules=rules)


def select_articles(question: str, catalog: str) -> list[int]:
    """
    조문 제목 목록을 보여주고 필요한 것만 고르게 합니다.

    조문 본문을 통째로 넣으면 요청당 3만 토큰을 쓰는데,
    제목만 보여주고 고르면 2천 토큰이면 됩니다.
    반환: 고른 번호 목록. 실패하면 빈 목록(=전체 사용).
    """
    obj, raw = call_json(select_prompt(question, catalog), SELECT_SCHEMA,
                         temperature=0, max_tokens=MAXTOK_SELECT, stage="select")
    if isinstance(obj, dict) and isinstance(obj.get("ids"), list):
        nums = []
        for n in obj["ids"]:
            try:
                n = int(n)
            except (TypeError, ValueError):
                continue
            if 1 <= n <= 9999 and n not in nums:
                nums.append(n)
        if len(nums) > SELECT_MAX:
            applog.step("조문 선별 상한", f"모델이 {len(nums)}개를 골라 앞 {SELECT_MAX}개만 씁니다")
        return nums[:SELECT_MAX]
    raw = _call(SELECT_PROMPT.format(question=question, catalog=catalog,
                                     output_rules=_SELECT_RULES_TEXT),
                temperature=0, max_tokens=MAXTOK_SELECT, stage="select")
    # 숫자만 뽑습니다. 모델이 설명을 덧붙여도 번호는 건집니다.
    # 다만 "제8조" 같은 조문 번호가 섞이지 않도록, 조/항/호 앞뒤 숫자는 제외합니다.
    cleaned = re.sub(r"제\s*\d+\s*(조|항|호)", " ", raw)
    nums = []
    for tok in re.findall(r"\d+", cleaned):
        n = int(tok)
        if 1 <= n <= 999 and n not in nums:
            nums.append(n)

    if LLM_DEBUG:
        _dbg(f"[select] 원문: {raw[:200]}\n[select] 선택: {nums[:SELECT_MAX]}")
    if len(nums) > SELECT_MAX:
        applog.step("조문 선별 상한", f"모델이 {len(nums)}개를 골라 앞 {SELECT_MAX}개만 씁니다")
    return nums[:SELECT_MAX]
