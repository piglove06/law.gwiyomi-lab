"""
LLM 호출부. Gemini 와 로컬 모델(Ollama 등)을 단계별로 골라 쓸 수 있습니다.

  .env 예시 — 답변 생성만 로컬로 돌리고 나머지는 Gemini
    LLM_BACKEND=gemini
    LLM_BACKEND_ANSWER=local
    LOCAL_BASE_URL=http://localhost:11434/v1
    LOCAL_MODEL=qwen3.5:9b-q4_K_M

[검증 상태]
  - 엔드포인트 URL 과 모델명은 제 기억 기반입니다. 반드시 확인하세요.
    https://ai.google.dev/gemini-api/docs  에서 현재 모델명을 보고 MODEL 을 맞추세요.
    모델명이 틀리면 404 가 납니다.
"""

import os
import re
import time

import httpx

# ── 인증키 ────────────────────────────────────────────────────
# 쉼표로 여러 개를 넣을 수 있습니다. 앞의 키가 한도(429)에 걸리면 다음 키로 넘어갑니다.
#   GEMINI_API_KEY=키1,키2,키3
# 한도에 걸린 키는 쿨다운 시간이 지나면 자동으로 다시 후보에 들어옵니다.
API_KEYS = [k.strip() for k in os.getenv("GEMINI_API_KEY", "").split(",") if k.strip()]
# 한도(429)에 걸린 키를 쉬게 할 시간(초).
# 분당 한도(TPM/RPM)는 1분이면 회복되므로 길게 잡을 이유가 없습니다.
# 짧게 두면 키를 빠르게 돌려쓸 수 있고, 응답에 retryDelay 가 오면 그 값을 우선합니다.
KEY_COOLDOWN = float(os.getenv("GEMINI_KEY_COOLDOWN", "15"))

# 키별 쿨다운 해제 시각 {키: unix time}
_key_blocked: dict[str, float] = {}


def _live_keys() -> list[str]:
    """지금 쓸 수 있는 키 목록. 쿨다운이 끝난 키는 자동 복귀합니다."""
    now = time.time()
    live = [k for k in API_KEYS if _key_blocked.get(k, 0) <= now]
    return live or API_KEYS          # 전부 막혔으면 그래도 한 번은 시도


def _block_key(key: str, seconds: float = 0, scope: str = ""):
    # 일일 한도(RPD)는 오늘 안에 안 풀리므로 길게 재웁니다.
    if scope == "day":
        seconds = max(seconds, 3600)
    _key_blocked[key] = time.time() + (seconds or KEY_COOLDOWN)
    alive = len(_live_keys())
    print(f"[gemini] 키 …{key[-6:]} 한도 초과 → {int(seconds or KEY_COOLDOWN)}초 대기 "
          f"(사용 가능 키 {alive}/{len(API_KEYS)})")


def key_status() -> dict:
    """화면에 표시할 키 상태."""
    now = time.time()
    return {
        "total": len(API_KEYS),
        "live": len(_live_keys()) if API_KEYS else 0,
        "blocked": [
            {"tail": k[-6:], "wait": int(_key_blocked[k] - now)}
            for k in API_KEYS if _key_blocked.get(k, 0) > now
        ],
    }


# ── 모델 ──────────────────────────────────────────────────────
# 단계마다 필요한 능력이 다릅니다. 비싼 모델이 필요한 곳에만 쓰세요.
#   용어 변환 : 쉬움   — 값싼 모델로 충분
#   되묻기    : 어려움 — 어디서 조문이 갈리는지 판단해야 함
#   답변 생성 : 중간   — 토큰을 가장 많이 먹는 단계
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
MODEL_TERMS   = os.getenv("GEMINI_MODEL_TERMS",   "") or MODEL
MODEL_CLARIFY = os.getenv("GEMINI_MODEL_CLARIFY", "") or MODEL
MODEL_ANSWER  = os.getenv("GEMINI_MODEL_ANSWER",  "") or MODEL
MODEL_SELECT  = os.getenv("GEMINI_MODEL_SELECT",  "") or MODEL

# 주 모델이 과부하(503)일 때 순서대로 시도할 대체 모델.
FALLBACKS = [m.strip() for m in os.getenv("GEMINI_FALLBACK", "").split(",") if m.strip()]

# ── 출력 토큰 상한 ────────────────────────────────────────────
# 상한이 없으면 모델이 계속 생성합니다.
# 실측: 용어 변환(두 줄이면 충분)에 4,243 토큰 55초, 되묻기에 9,551 토큰 127초.
# 단계마다 필요한 분량이 다르므로 따로 잡습니다.
MAXTOK_TERMS   = int(os.getenv("MAXTOK_TERMS",   "300"))    # 두 줄
MAXTOK_SELECT  = int(os.getenv("MAXTOK_SELECT",  "200"))    # 숫자 나열
MAXTOK_CLARIFY = int(os.getenv("MAXTOK_CLARIFY", "800"))    # 질문 몇 줄
MAXTOK_ANSWER  = int(os.getenv("MAXTOK_ANSWER",  "4000"))   # 근거 + 설명

RETRIES = 3          # 503 재시도 횟수
BACKOFF = 2.0        # 재시도 간격(초). 시도마다 2배로 늘어납니다.

BASE = "https://generativelanguage.googleapis.com/v1beta/models"
TIMEOUT = 60.0

# ── 로컬 LLM (Ollama 등 OpenAI 호환 서버) ─────────────────────
# Ollama 는 /v1 경로로 OpenAI 호환 API 를 제공합니다.
#   LOCAL_BASE_URL=http://localhost:11434/v1
#   LOCAL_MODEL=qwen3.5:9b-q4_K_M
LOCAL_BASE_URL = os.getenv("LOCAL_BASE_URL", "http://localhost:11434/v1").rstrip("/")
LOCAL_MODEL = os.getenv("LOCAL_MODEL", "")
LOCAL_TIMEOUT = float(os.getenv("LOCAL_TIMEOUT", "300"))   # 로컬은 느리므로 넉넉히

# 사고 과정(Thinking)을 끌지. 이 프로그램은 형식 준수가 중요하지
# 추론이 필요한 작업이 아니므로 끄는 편이 훨씬 빠릅니다.
# 1 이면 Ollama 네이티브 /api/chat 에 think=false 를 보냅니다.
# (프롬프트에 /no_think 를 붙이는 방식은 Qwen3.5 에서 듣지 않았습니다)
# Ollama 가 아닌 서버를 쓰면 0 으로 두세요.
LOCAL_NO_THINK = os.getenv("LOCAL_NO_THINK", "1") not in ("0", "", "false", "False")

# 1 이면 프롬프트와 응답 전문을 서버 콘솔에 찍습니다.
# 로컬 모델이 형식을 어떻게 어기는지 확인할 때 켜세요.
LLM_DEBUG = os.getenv("LLM_DEBUG", "0") not in ("0", "", "false", "False")


# 단계별로 어느 백엔드를 쓸지. gemini | local
# 비워두면 LLM_BACKEND 값을 따르고, 그것도 없으면 gemini 입니다.
_DEFAULT_BACKEND = os.getenv("LLM_BACKEND", "gemini").strip().lower()


def _backend(stage: str) -> str:
    v = os.getenv(f"LLM_BACKEND_{stage.upper()}", "").strip().lower()
    return v or _DEFAULT_BACKEND


BACKEND_TERMS   = _backend("terms")
BACKEND_SELECT  = _backend("select")
BACKEND_CLARIFY = _backend("clarify")
BACKEND_ANSWER  = _backend("answer")

# 로컬 백엔드에서 쓸 모델. 단계별로 다르게 지정할 수 있습니다.
LOCAL_MODEL_TERMS   = os.getenv("LOCAL_MODEL_TERMS",   "") or LOCAL_MODEL
LOCAL_MODEL_SELECT  = os.getenv("LOCAL_MODEL_SELECT",  "") or LOCAL_MODEL
LOCAL_MODEL_CLARIFY = os.getenv("LOCAL_MODEL_CLARIFY", "") or LOCAL_MODEL
LOCAL_MODEL_ANSWER  = os.getenv("LOCAL_MODEL_ANSWER",  "") or LOCAL_MODEL


class AiError(Exception):
    pass


class QuotaError(AiError):
    """무료 한도 초과. scope: "minute"(분당) | "day"(일일) | "unknown" """

    def __init__(self, message: str, scope: str = "unknown", retry_after: int = 60):
        super().__init__(message)
        self.scope = scope
        self.retry_after = retry_after


# --- 프롬프트 --------------------------------------------------------
# 이 프로그램의 핵심입니다. 여기가 부실하면 AI가 아는 척하며 지어냅니다.

TERM_PROMPT = """너는 대한민국 법령 검색을 돕는 도구다.
질문자는 지방자치단체 환경 담당 공무원이다.

사용자의 일상적인 질문을 법령에서 실제로 쓰이는 용어로 바꾸는 일만 한다.
설명·인사·사고 과정을 쓰지 마라. 아래 두 줄만 출력한다.

법령명: (실제 법령 이름 1~2개, 쉼표로 구분)
용어: (실제 법령용어 3~5개, 쉼표로 구분)

규칙:
- **반드시 실제로 존재하는 법령 이름과 용어만 쓴다.**
- 같은 용어가 여러 법에 쓰일 때는 환경 분야 법령을 우선한다.
  예) "누출검사" → 토양환경보전법이 1순위, 위험물안전관리법이 2순위.
- 법령명은 정식 명칭으로 쓴다. "시행령", "시행규칙" 은 붙이지 않는다.
- **질문이 일반적인 상황이면 일반법을 고르라.**
  질문에 명시되지 않은 특수·예외 분야 법을 1순위로 올리지 마라.
  예) "사업장에서 폐기물을 배출한다" → 폐기물관리법 (O)
      방사성폐기물 관리법 (X — 질문에 방사성이라는 말이 없다)
      "폐수를 배출한다" → 물환경보전법 (O), 해양환경관리법 (X)
- 영어를 쓰지 마라. 한국어 법령 용어만 쓴다.
- 백틱(`), 따옴표, 괄호, 번호를 붙이지 마라.

예시 1)
질문: 주유소 땅이 기름으로 오염됐는지 조사하는 절차
법령명: 토양환경보전법, 위험물안전관리법
용어: 특정토양오염관리대상시설, 토양정밀조사, 토양오염도검사

예시 2)
질문: 누출검사 검사주기가 어떻게 되나
법령명: 토양환경보전법, 위험물안전관리법
용어: 누출검사, 특정토양오염관리대상시설, 정기검사

예시 3)
질문: 폐수 배출시설 신고를 안 하면 어떻게 되나
법령명: 물환경보전법
용어: 폐수배출시설, 배출시설 설치신고, 과태료

질문: {question}
"""


ANSWER_PROMPT = """너는 지방자치단체 공무원이 법령을 확인할 때 쓰는 조문 조회 도우미다.
사용자는 법률 전문가가 아니다. 쉽게 설명하되, 근거는 [조문 원문] 안에서만 찾는다.

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

━━ 기간 계산 ━━━━━━━━━━━━━━━━━━━━━━━━━━━
★ **"설치 후 몇 년" 과 "마지막 검사 후 몇 년" 은 다른 것이다.**
  조문이 어느 날을 기준으로 세라고 하는지 확인하고 그 날을 기준일로 삼는다.
    · "설치한 날부터"        → 설치일 기준
    · "직전 검사를 받은 날부터" → 마지막 검사일 기준
- 사용자가 기준일을 알려줬고 조문에 주기가 있으면 **계산해서 날짜를 말하라.**
  예) 마지막 검사일 2025-09-15 + 주기 8년 → 2033-09-15
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
질문에 대한 답을 __먼저__ 2~3줄로 말한다. 담당자가 민원인에게 그대로
읽어줄 수 있는 문장으로 쓴다. 조문번호를 나열하지 마라.
조건이 "모름" 이라 하나로 정할 수 없으면 "○○이면 A, □□이면 B" 로 쓴다.
여기서도 수치는 **굵게**, 의무는 __밑줄__ 로 표시한다.
예) 설치 전에 __관할 시장·군수·구청장에게 신고해야 합니다__.
    신고는 시설 설치 **전**에 해야 하고, 변경 시에는 **30일 이내**입니다.

【적용 조건】
사용자가 알려준 사실만 한 줄씩 적는다. 안 알려준 항목은 **줄째로 빼라**
(‑ 미확인 을 줄줄이 적으면 화면만 길어진다).
해당하는 것만 골라 쓴다: 시설 / 용량 / 설치 경과 / 검사 종류 / 마지막 검사 / 기타
예)
- 시설: 주유소 지하매설 저장시설
- 용량: 30,000L
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
  기준일 2025-09-15 (직전 검사일, 「…시행규칙」 제○조)
  + 주기 **8년** (「…시행규칙」 별표 4)
  = 다음 검사 **2033-09-15** 까지
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


class BusyError(AiError):
    """모델 과부하(503). 잠시 후 재시도하면 대개 풀립니다."""


def _once(model: str, prompt: str, temperature: float, key: str,
          max_tokens: int = 0) -> str:
    """모델 한 번 호출. 실패는 예외로 올립니다."""
    url = f"{BASE}/{model}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            **({"maxOutputTokens": max_tokens} if max_tokens else {}),
        },
    }
    try:
        resp = httpx.post(url, params={"key": key}, json=payload, timeout=TIMEOUT)
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code == 429:
            raise _parse_quota(e.response) from e
        if code in (500, 502, 503, 504):
            raise BusyError(f"{model} 과부하/일시장애 ({code})") from e
        raise AiError(
            f"Gemini 호출 실패 ({code}). 모델명({model})이나 API 키를 확인하세요. "
            f"응답: {e.response.text[:250]}"
        ) from e
    except httpx.HTTPError as e:
        raise BusyError(f"{model} 연결 실패: {e}") from e

    data = resp.json()
    try:
        return _clean_output(data["candidates"][0]["content"]["parts"][0]["text"])
    except (KeyError, IndexError) as e:
        raise AiError(f"Gemini 응답 형식이 예상과 다릅니다: {str(data)[:300]}") from e


def _ollama_native_base() -> str:
    """OpenAI 호환 주소에서 Ollama 네이티브 주소를 만듭니다.
    http://localhost:11434/v1  ->  http://localhost:11434
    """
    return re.sub(r"/v1/?$", "", LOCAL_BASE_URL)


def _call_local(prompt: str, temperature: float, model: str,
                max_tokens: int = 0) -> str:
    """
    로컬 LLM 에 요청합니다.

    LOCAL_NO_THINK=1 이면 Ollama 네이티브 API(/api/chat)를 쓰고 think=false 를 보냅니다.
      · 프롬프트에 /no_think 를 붙이는 방식은 Qwen3.5 에서 동작하지 않았습니다.
        (server.log 확인 결과 사고 과정이 그대로 생성됨)
      · think 는 API 파라미터라 모델 지시문과 달리 확실히 적용됩니다.
    그 외에는 OpenAI 호환 /chat/completions 를 씁니다.
    """
    if not model:
        raise AiError(
            "LOCAL_MODEL 이 비어 있습니다. .env 에 LOCAL_MODEL=qwen3.5:9b-q4_K_M 처럼 넣으세요."
        )

    native = LOCAL_NO_THINK
    if native:
        url = f"{_ollama_native_base()}/api/chat"
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
    else:
        url = f"{LOCAL_BASE_URL}/chat/completions"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "stream": False,
            **({"max_tokens": max_tokens} if max_tokens else {}),
        }

    try:
        resp = httpx.post(url, json=payload, timeout=LOCAL_TIMEOUT)
        resp.raise_for_status()
    except httpx.ConnectError as e:
        raise AiError(
            f"로컬 LLM 서버에 연결하지 못했습니다 ({url}). "
            f"Ollama 가 실행 중인지 확인하세요. ({e})"
        ) from e
    except httpx.HTTPStatusError as e:
        body = e.response.text[:300]
        if e.response.status_code == 404:
            raise AiError(
                f"모델 '{model}' 을(를) 찾지 못했습니다. "
                f"'ollama list' 로 설치된 이름을 확인하세요. 응답: {body}"
            ) from e
        if e.response.status_code == 400 and native:
            raise AiError(
                f"이 서버가 think 옵션을 받지 않습니다. "
                f".env 에서 LOCAL_NO_THINK=0 으로 두고 다시 시도하세요. 응답: {body}"
            ) from e
        print(f"[local] HTTP {e.response.status_code} — 요청 model={model} "
              f"max_tokens={max_tokens}\n  응답: {body}", flush=True)
        raise AiError(f"로컬 LLM 오류 ({e.response.status_code}): {body}") from e
    except httpx.HTTPError as e:
        raise AiError(f"로컬 LLM 호출 실패: {e}") from e

    data = resp.json()
    if native:                                   # /api/chat 응답 구조
        msg = data.get("message") or {}
        text = msg.get("content") or ""
        if not text.strip():
            text = msg.get("thinking") or ""
    else:                                        # /chat/completions 응답 구조
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        text = msg.get("content") or ""
        if not text.strip():
            text = msg.get("reasoning_content") or msg.get("reasoning") or ""

    if not text.strip():
        raise AiError(f"로컬 LLM 이 빈 응답을 돌려줬습니다: {str(data)[:400]}")

    # 상한에 걸려 잘렸는지 알려줍니다. 잘리면 형식이 깨져 파싱이 실패합니다.
    done = data.get("done_reason") if native else \
        ((data.get("choices") or [{}])[0].get("finish_reason"))
    if done in ("length", "MAX_TOKENS"):
        print(f"[local] ⚠ 출력이 상한({max_tokens})에서 잘렸습니다. "
              f".env 의 MAXTOK_* 를 올리거나 프롬프트를 줄이세요.", flush=True)

    raw = text
    text = _clean_output(text)

    if LLM_DEBUG:
        think = (data.get("message") or {}).get("thinking") if native else None
        print(f"\n[local:{model}] ({'native/think=false' if native else 'openai'}) "
              f"── 응답 원문 ──\n{raw[:1200]}"
              + (f"\n[local] ── thinking 필드 ──\n{str(think)[:300]}" if think else "")
              + f"\n[local] ── 정리 후 ──\n{text[:600]}\n", flush=True)
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


def _call(prompt: str, temperature: float = 0.3, model: str = "",
          backend: str = "", max_tokens: int = 0) -> str:
    """
    키 순회 → 모델 재시도 → 대체 모델 순으로 시도합니다.

    - 429(한도 초과)  : 그 키를 쿨다운 목록에 넣고 다음 키로 넘어갑니다.
    - 503(과부하)     : 구글 쪽 사정이라 키를 바꿔도 소용없습니다. 잠깐 쉬고 재시도.
    """
    if (backend or _DEFAULT_BACKEND) == "local":
        return _call_local(prompt, temperature, model, max_tokens)

    if not API_KEYS:
        raise AiError("GEMINI_API_KEY 가 없습니다. .env 를 확인하세요.")

    models = [model or MODEL] + [m for m in FALLBACKS if m != (model or MODEL)]
    last_busy = None
    last_quota = None

    for key in _live_keys():
        for md in models:
            wait = BACKOFF
            for attempt in range(RETRIES):
                try:
                    return _once(md, prompt, temperature, key, max_tokens)
                except QuotaError as e:
                    _block_key(key, e.retry_after, e.scope)
                    last_quota = e
                    break                       # 다음 키로
                except BusyError as e:
                    last_busy = e
                    if attempt < RETRIES - 1:
                        time.sleep(wait)
                        wait *= 2
            else:
                continue                        # 재시도 소진 → 다음 모델
            if last_quota is not None:
                break                           # 한도면 모델 바꿔도 소용없음
        last_quota = None                       # 다음 키에서는 새로 판단

    if last_busy is not None:
        raise AiError(
            f"Gemini 서버가 계속 응답하지 않습니다 ({last_busy}). "
            f"모델 과부하일 가능성이 높습니다. 잠시 후 다시 시도해 주세요."
        )
    st = key_status()
    raise QuotaError(
        f"등록된 키 {st['total']}개가 모두 한도에 도달했습니다.",
        scope="minute",
        retry_after=min([b["wait"] for b in st["blocked"]] or [60]),
    )


def _parse_quota(resp) -> QuotaError:
    """
    429 응답에서 분당 한도인지 일일 한도인지 가려냅니다.

    Gemini 는 에러 본문에 quotaId / quotaMetric 같은 필드로 어떤 한도인지 알려주고,
    RetryInfo 에 retryDelay 를 담아 보내는 경우가 있습니다.
    형식이 바뀔 수 있으므로 문자열 매칭으로 방어적으로 읽습니다.
    """
    body = resp.text or ""
    low = body.lower()

    scope, retry = "unknown", 60
    if "perday" in low or "per_day" in low or "daily" in low:
        scope = "day"
    elif "perminute" in low or "per_minute" in low or "perminute" in low:
        scope = "minute"
        retry = 60

    # retryDelay: "37s" 형태
    m = re.search(r'"retrydelay"\s*:\s*"(\d+)s"', low)
    if m:
        retry = int(m.group(1))
        if scope == "unknown":
            scope = "minute" if retry <= 300 else "day"

    return QuotaError("무료 사용 한도에 도달했습니다.", scope=scope, retry_after=retry)


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
    return bool(re.search(r"(법|령|규칙|조례|고시|지침|예규|훈령)$", s))


def extract_terms(question: str) -> dict:
    """
    질문 -> 검색어 후보. 반환: {"법령명": [...], "용어": [...]}

    로컬 모델은 형식을 자주 어깁니다. 라벨이 있으면 그것을 쓰고,
    없으면 줄 단위로 훑어 법령처럼 생긴 것과 아닌 것을 나눕니다.
    """
    raw = _call(TERM_PROMPT.format(question=question), temperature=0,
                model=(LOCAL_MODEL_TERMS if BACKEND_TERMS == "local" else MODEL_TERMS),
                backend=BACKEND_TERMS, max_tokens=MAXTOK_TERMS)

    out = {"법령명": [], "용어": []}

    def add(key: str, chunk: str):
        for item in re.split(r"[,、/·]|\s{2,}", chunk):
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
            for item in re.split(r"[,、/·]", line):
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
        print(f"[terms] 파싱 결과: {out}", flush=True)

    return out


def answer(question: str, context: str) -> str:
    """조문 원문을 근거로 답변 생성."""
    return _call(ANSWER_PROMPT.format(question=question, context=context),
                 model=(LOCAL_MODEL_ANSWER if BACKEND_ANSWER == "local" else MODEL_ANSWER),
                 backend=BACKEND_ANSWER, max_tokens=MAXTOK_ANSWER)


CLARIFY_PROMPT = """너는 지방자치단체 환경 담당 공무원의 법령 질문을 다듬는 도구다.
질문자는 법령에 익숙하지 않다. 무엇을 특정해야 조문이 정해지는지 스스로 모른다.
그러므로 적극적으로 되물어 질문의 품질을 끌어올려라.

[조문 목록] 은 이 질문에 대해 실제로 수집된 법령의 조문 제목이다.
**이 목록에 실제로 있는 내용만 근거로 질문하라.**

절대 지킬 것:
- **[조문 목록]에 없는 제도·시설·용어를 질문에 쓰지 마라.**
  네 기억에 있는 다른 법의 개념을 끌어오지 마라.
  예) 조문 목록이 토양환경보전법뿐인데 "VOC 배출시설", "유해화학물질 취급시설" 을
      선택지로 넣으면 안 된다. 그것은 다른 법의 용어다.
- 용어는 조문에 적힌 **정식 명칭 그대로** 쓴다.
  예) "특정토양오염유발시설"(X) → "특정토양오염관리대상시설"(O)
- 조문에서 근거를 찾을 수 없는 구분은 묻지 마라.

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
**기간**을 묻는 것이면 다음을 반드시 확인한다.
  1) 법정 검사 대상인지          2) 검사 종류가 무엇인지
  3) 최초 검사인지 정기검사인지  4) **직전 검사를 언제 받았는지**
  5) 다음 기한을 세는 **기준일**이 무엇인지

★ **"설치한 지 15년 됐다" 와 "2025년 9월에 검사를 받았다" 는 완전히 다른
  정보다. 절대 같은 것으로 취급하지 마라.**
    · 설치 경과연수 → 대상 여부·최초검사 여부를 가른다
    · 마지막 검사일 → **다음 정기검사일을 계산한다**
  질문자가 기간을 물었는데 마지막 검사일이 [이미 답변된 조건]에 없으면,
  그것을 **최우선으로** 묻는다. 설치연수만 물어보고 끝내면 안 된다.

★ 날짜는 보기로 만들 수 없다. 화면에 자유 입력 칸이 따로 있으므로
  보기를 이렇게 만든다.
      마지막 누출검사를 언제 받았습니까?|아래 칸에 날짜 입력|받은 적 없음|모름

더 물을 것이 없으면 딱 한 줄만 출력한다.
OK

물을 것이 남았으면 아래 형식으로만 출력한다. 다른 말은 쓰지 않는다.
질문|보기1|보기2|보기3

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

- **"…에 해당하나요?" 처럼 한쪽만 주지 마라. 반대쪽도 반드시 넣어라.**
  한쪽만 주면 사용자가 "아니다" 를 고를 수 없다.
  나쁜 예) 방사성폐기물에 해당하나요?|방사성폐기물|모름
  좋은 예) 방사성폐기물에 해당하나요?|방사성폐기물|아니오|모름
  (구체적인 말이 있으면 "예" 보다 그 말을 쓴다. 무엇에 예인지 바로 보인다.)

규칙:
- 한 번에 최대 3개까지 물을 수 있다. 한 줄에 하나.
  많이 묻는 것보다 **결정적인 것을 먼저 묻는 것**이 중요하다.
- 보기는 2~4개. 각 보기는 14자 이내. 마지막 보기로 "모름" 을 넣어라.
- 실제로 적용 조문·기준·기한이 달라지는 것만 묻는다.
- [이미 답변된 조건] 에 나온 것은 절대 다시 묻지 마라. 다르게 표현해서도 묻지 마라.
- 남은 갈래가 없으면 반드시 OK 를 출력하고 끝내라.
  남은 것이 없는데 계속 묻는 것이 더 나쁘다.

[질문]
{question}

[이미 답변된 조건]
{answered}

[조문 목록]
{catalog}
"""


# "…에 해당하나요?" 같은 예/아니오 질문인지 판별합니다.
# 예/아니오로 답하는 질문의 어미.
# ★ 2026-08-18 — "지정되었나요?" 처럼 `되었나요`·`했나요`·`인가?` 로 끝나는
#   물음이 빠져 있어서, 부정 보기가 없는 채로 화면에 나갔습니다
#   ("시설이 …로 지정되었나요?  ○ 지정됨  ○ 모름" — 아니라고 답할 방법이 없음).
_YESNO_RE = re.compile(
    r"(해당하나요|해당합니까|인가요|입니까|맞나요|있나요|없나요|하나요|"
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


def clarify(question: str, answered: str = "", catalog: str = "") -> list[dict]:
    """
    질문이 애매하면 선택지를 돌려줍니다. 더 물을 게 없으면 빈 목록.

    catalog 에는 실제로 수집된 조문 제목 목록을 넘깁니다.
    이것이 없으면 AI 가 기억에 의존해 존재하지 않는 용어를 지어냅니다.
    (실제 사례: "특정토양오염유발시설" — 법령에 없는 이름)
    """
    raw = _call(CLARIFY_PROMPT.format(
        question=question, answered=answered or "(없음)",
        catalog=catalog or "(아직 수집된 조문이 없습니다. 일반적인 표현으로만 물으세요.)"),
        model=(LOCAL_MODEL_CLARIFY if BACKEND_CLARIFY == "local" else MODEL_CLARIFY),
        backend=BACKEND_CLARIFY, max_tokens=MAXTOK_CLARIFY).strip()

    # "OK" 만 나오면 더 물을 게 없다는 뜻입니다.
    # 로컬 모델은 "OK." "OK 입니다" 처럼 덧붙이기도 합니다.
    head = raw.strip().splitlines()[0].strip() if raw.strip() else ""
    if re.match(r"^ok\b", head, re.I) or "|" not in raw:
        if LLM_DEBUG:
            print(f"[clarify] 더 물을 것 없음 (응답: {head[:60]})", flush=True)
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
            print(f"[clarify] 질문 정리: {q}  →  {q2}", flush=True)
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
                print(f"[clarify] 보기가 '할 일' 문장 → 질문 {len(todo_split)}개로 폄",
                      flush=True)
            out.extend(todo_split)
            continue

        # ★ 질문과 보기가 둘 다 물음인 줄("A인가?|B인가?|모름")을 먼저 봅니다.
        #   _repair_a_or_b 보다 앞에 둡니다 — 그쪽은 보기의 질문을 **버리는**
        #   방향이라, 여기서 살릴 수 있는 선택지가 먼저 사라집니다.
        fixed = _repair_all_questions(q, opts)
        if fixed:
            if LLM_DEBUG:
                print(f"[clarify] 질문·보기가 모두 물음 → 보기로 재구성: "
                      f"{fixed['question']} / {', '.join(fixed['options'])}", flush=True)
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
    cleaned = []
    for item in out:
        left = [o for o in item["options"] if not _is_todo_option(o)]
        if len(left) < len(item["options"]) and LLM_DEBUG:
            print(f"[clarify] '할 일' 보기 {len(item['options']) - len(left)}개 제거: "
                  f"{item['question'][:30]}", flush=True)
        # 잘린 조각이 남지 않게 너무 짧은 보기도 버립니다.
        # 단 "예"/"네" 는 한 글자여도 정상 보기입니다.
        left = [o for o in left
                if len(o.strip()) >= 2 or o.strip() in ("예", "네")]
        if len(left) >= 2 and re.search(r"[가-힣]", item["question"]):
            cleaned.append({"question": item["question"], "options": left})
    out = cleaned

    if LLM_DEBUG:
        print(f"[clarify] {len(out)}개 질문 파싱 (원문 {len(raw.splitlines())}줄)", flush=True)
    return out[:6]


SELECT_PROMPT = """너는 대한민국 법령 조문을 골라내는 도구다.

아래 [조문 목록] 은 법령명과 조문 번호·제목만 나열한 것이다.
[질문] 에 답하려면 어떤 조문의 본문을 읽어야 하는지 고르라.

출력 형식 — 고른 번호만 쉼표로 나열한다. 다른 말은 쓰지 않는다.
3,7,12,25

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


def select_articles(question: str, catalog: str) -> list[int]:
    """
    조문 제목 목록을 보여주고 필요한 것만 고르게 합니다.

    조문 본문을 통째로 넣으면 요청당 3만 토큰을 쓰는데,
    제목만 보여주고 고르면 2천 토큰이면 됩니다.
    반환: 고른 번호 목록. 실패하면 빈 목록(=전체 사용).
    """
    raw = _call(SELECT_PROMPT.format(question=question, catalog=catalog),
                temperature=0,
                model=(LOCAL_MODEL_SELECT if BACKEND_SELECT == "local" else MODEL_SELECT),
                backend=BACKEND_SELECT, max_tokens=MAXTOK_SELECT)
    # 숫자만 뽑습니다. 모델이 설명을 덧붙여도 번호는 건집니다.
    # 다만 "제8조" 같은 조문 번호가 섞이지 않도록, 조/항/호 앞뒤 숫자는 제외합니다.
    cleaned = re.sub(r"제\s*\d+\s*(조|항|호)", " ", raw)
    nums = []
    for tok in re.findall(r"\d+", cleaned):
        n = int(tok)
        if 1 <= n <= 999 and n not in nums:
            nums.append(n)

    if LLM_DEBUG:
        print(f"[select] 원문: {raw[:200]}\n[select] 선택: {nums[:40]}", flush=True)
    return nums[:40]
