"""
FastAPI 서버.

C# 으로 치면 Program.cs + Controller 를 합쳐놓은 파일입니다.
@app.get("/경로") 데코레이터가 [HttpGet("경로")] 어트리뷰트와 같은 역할입니다.

실행:  uvicorn main:app --reload
접속:  http://127.0.0.1:8000
"""

import asyncio
import hashlib
import hmac
import json
import urllib.parse
from datetime import datetime, timedelta, timezone
import logging
import os
import re
import secrets
import time

# ── 서버 로그 형식 ──────────────────────────────────────────────
# ★ 2026-09-29 — 시각을 서울 시간(KST) "[MM-DD HH:MM:SS]" 로 통일합니다.
#   PC 시간대 설정과 무관하게 찍히고, 질문 블록 로그(applog.py)와 모양이 같습니다.
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-7s %(message)s",
    datefmt="%m-%d %H:%M:%S",
)
_KST = timezone(timedelta(hours=9))
for _h in logging.getLogger().handlers:
    if _h.formatter:
        _h.formatter.converter = lambda secs: datetime.fromtimestamp(secs, _KST).timetuple()
for _n in ("uvicorn", "uvicorn.access", "uvicorn.error"):
    _lg = logging.getLogger(_n)
    _lg.handlers.clear()          # uvicorn 기본 포맷을 제거하고
    _lg.propagate = True          # 위 설정을 따르게 합니다


class _QuietAccess(logging.Filter):
    """
    접근 로그에서 반복되는 정상 요청을 숨깁니다.
      /static·/favicon  화면 파일
      /api/version      화면과 자동 테스트 감시자가 수시로 부름
      /api/ask          질문은 applog 의 START/END 블록으로 따로 보여줌
    오류(4xx/5xx)는 항상 보여줍니다.
    """
    QUIET = ("/static/", "/favicon", "/api/version", "/api/ask")

    def filter(self, record):
        try:
            path, status = str(record.args[2]), int(record.args[4])
            return not (status < 400 and path.startswith(self.QUIET))
        except Exception:                                  # noqa: BLE001
            return True


logging.getLogger("uvicorn.access").addFilter(_QuietAccess())
# LLM·법제처 호출마다 찍히던 "HTTP Request: POST …" 줄을 숨깁니다 (오류는 그대로 보임).
for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from dotenv import load_dotenv

load_dotenv()  # .env 파일을 읽어 환경변수로 올립니다. import 순서 주의: 다른 모듈보다 먼저.

from fastapi import FastAPI, Form, Request  # noqa: E402
from fastapi.responses import (  # noqa: E402
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import ai_client  # noqa: E402
import applog  # noqa: E402
import law_client  # noqa: E402
import pdf_maker  # noqa: E402
import intent as intent_mod  # noqa: E402

# 되묻기 최대 라운드. .env 의 CLARIFY_ROUNDS 로 조절합니다.
# 라운드마다 LLM 호출이 1회 늘어납니다.
# ★ v1.31 — 6 → 3. 되묻기는 결론을 가르는 사실만 묻도록 바뀌었고(본문 기반),
#   의도별로 아예 안 묻는 질문도 생겨 6번까지 왕복할 일이 없습니다.
CLARIFY_ROUNDS = int(os.getenv("CLARIFY_ROUNDS", "3"))

# 되묻기 판단에 넘길 조문 목록 줄 수.
# 로컬 모델은 입력이 길수록 급격히 느려지므로 앞부분만 보여줍니다.
CLARIFY_CATALOG_LINES = int(os.getenv("CLARIFY_CATALOG_LINES", "60"))   # v1.31 부터 미사용(호환용)

# ★ v1.31 — 되묻기에 넘기는 **선별 조문 본문**의 토큰 상한.
#   v1.30 까지는 조문 제목 60줄만 넘겼습니다. 본문을 넣어야 기준값(2만L·1년·3회)을
#   보고 보기를 나눌 수 있습니다. 지시문(약 2.5천) + 이 값 + 출력(800) 이 컨텍스트 안에 듭니다.
CLARIFY_CTX_TOKENS = int(os.getenv("CLARIFY_CTX_TOKENS", "5000"))

# ★ v1.32 — 별표 본문을 AI 에 넘길 때의 글자 상한 (표 테두리·공백을 걷어낸 **뒤** 기준).
#   v1.31 까지는 원문을 그대로 2,000자에서 잘랐습니다. 법제처 별표는 표 테두리(─│┼)와
#   정렬용 공백이 절반 이상이라, 소음 규제기준(시행규칙 별표 8) 은 표만 들어가고
#   **비고(작업시간 +5dB, 공휴일 −5dB 보정)** 가 통째로 잘렸습니다.
#   → 68dB 을 65dB 와 비교해 "초과" 로 답한 원인.
#   이제 테두리·공백을 걷어내고(같은 별표가 6천 → 2천 자), 그래도 길면 비고를 남깁니다.
BP_CTX_CHARS = int(os.getenv("BP_CTX_CHARS", "3500"))
_BOX_CHARS = re.compile(r"[─━┌┐└┘├┤┬┴┼┏┓┗┛┣┫┳┻╋═║╔╗╚╝╠╣╦╩╬]+")


def _compact_bp(text: str) -> str:
    """별표 원문에서 표 테두리·정렬 공백·빈 줄을 걷어냅니다. 글자(내용)는 그대로입니다."""
    t = _BOX_CHARS.sub("", str(text or ""))
    t = re.sub(r"[ \t　 ]+", " ", t)
    lines = []
    for ln in t.split("\n"):
        ln = ln.strip()
        if not ln.strip("│| "):            # 칸 구분자만 남은 줄
            continue
        lines.append(re.sub(r"\s*│\s*", "│", ln))
    return "\n".join(lines)


def _fit_keep_notes(text: str, limit: int) -> str:
    """
    limit 자 안으로 줄이되, 표 뒤의 **비고**(보정·예외·적용 범위)는 남깁니다.
    비고는 표의 숫자를 바꾸는 규정이라 잘리면 답이 틀립니다 (예: 공사장 소음 +5dB).
    """
    t = str(text or "")
    if len(t) <= limit:
        return t
    i = t.find("비고")
    if i > limit * 0.4:                      # 비고가 상한 밖으로 밀려나는 경우만
        notes = t[i:i + int(limit * 0.5)]
        head = t[:max(200, limit - len(notes) - 30)]
        tail = "\n…(이하 생략)" if i + len(notes) < len(t) else ""
        return head + "\n…(표 일부 생략)…\n" + notes + tail
    return t[:limit] + "\n…(이하 생략)"

# 답변 생성 때 출력(답변)용으로 남겨 둘 토큰 수.
# ★ 2026-09-29 — 예전에는 조문을 "6만 자" 로 잘랐습니다(CONTEXT_LIMIT, 클라우드 모델 시절 값).
#   로컬 LLM 컨텍스트(16,384토큰)에는 처음부터 안 맞아 답변 단계가 400 으로 실패했습니다.
#   이제 llama-server 의 컨텍스트 길이에서 [지시문 + 이 값] 을 뺀 만큼만 조문을 넣습니다.
#   (MAXTOK_ANSWER 보다 크게 잡을 필요는 없습니다)
ANSWER_RESERVE = int(os.getenv("ANSWER_RESERVE", "3000"))

# ★ v1.32 — 체계도로 수집할 법령(법률 + 시행령·시행규칙 묶음) 수. 2 → 3.
#   후보: 지능형 검색 1위 → AI 추측 중 지능형 검색에도 나온 것 → 지능형 검색 2위 → 나머지 AI 추측.
#   늘리면 정답 법령을 잡을 확률은 오르고, 조문 목록이 길어져 선별이 느려집니다(질문당 약 10~20초).
LAW_CANDIDATES = int(os.getenv("LAW_CANDIDATES", "3"))

# ★ v1.33 — 법령이 애매하면 사용자에게 "어느 법" 인지 묻습니다(공무원 사용자는 큰 틀의 법을 압니다).
#   애매 = 지능형 검색 1위 법령의 점수 비중이 LAW_ASK_SHARE 미만 **이고** AI 추측 법령과 1위가 다를 때.
#   (2026-10-08 평가 25개 기준: 누출검사 2건만 해당, 나머지 21건은 해당 없음·법령 전부 정답)
LAW_ASK_SHARE = float(os.getenv("LAW_ASK_SHARE", "0.5"))
LAW_Q = "어느 법의 규정을 찾으시나요?"
LAW_Q_DIRECT = "직접 입력 (아래 '추가로 알려줄 내용' 칸에 법령명)"
LAW_Q_UNKNOWN = "모름 (모두 찾아보기)"
_LAW_TAIL_RE = re.compile(r"(법|법률|시행령|시행규칙|규칙|령)$")


def _laws_mode(req) -> str:
    return "prefer" if str(getattr(req, "laws_mode", "") or "").lower() == "prefer" else "only"


def _user_laws(req) -> list:
    """사용자가 지정한 법령 이름 — 질문 화면의 "참고할 법령" 칸 + 되묻기 "어느 법" 답. 최대 3개."""
    out = []
    for x in (getattr(req, "laws", None) or []):
        x = str(x).strip().strip("「」『』\"' ")
        # "대기환경보전법 제23조제1항" 처럼 조문까지 적어도 법령 이름만 씁니다(조문은 질문에 적는 칸).
        x = re.sub(r"\s*제\s*\d+\s*조.*$", "", x).strip().strip("「」『』\"' ")
        if x:
            out.append(x)
    ans = str(getattr(req, "answered", "") or "")
    m = re.search(re.escape(LAW_Q) + r"\s*:\s*([^/]+)", ans)
    if m:
        v = m.group(1).strip()
        if v.startswith("직접 입력"):
            for m2 in re.finditer(r"추가 설명:\s*([^/]+)", ans + " / 추가 설명: " + str(req.note or "")):
                for t in re.split(r"[,，、]|\s{2,}", m2.group(1)):
                    t = t.strip().strip("「」『』\"' ")
                    if t and _LAW_TAIL_RE.search(t):
                        out.append(t)
        elif not v.startswith("모름"):
            out.append(v.strip("「」『』\"' "))
    seen, uniq = set(), []
    for x in out:
        k = _law_key(x) if "_law_key" in globals() else x.replace(" ", "")
        if k not in seen:
            seen.add(k)
            uniq.append(x)
    return uniq[:3]

# 조문 선별 사용 여부. 0 이면 예전처럼 전체 조문을 넣습니다.
SELECT_ARTICLES = os.getenv("SELECT_ARTICLES", "1") not in ("0", "false", "False")

# 자치법규(조례·규칙)를 찾을 지자체.
# 법제처 조례 검색은 다른 지자체 결과까지 섞어 돌려주므로,
# 조회 후 지자체기관명으로 한 번 더 걸러냅니다.
LOCAL_GOV = os.getenv("LOCAL_GOV", "성남시").strip()

VERSION = "1.33"

app = FastAPI(title="법령 조회 도우미", version=VERSION)

# static 폴더를 /static 경로로 서비스합니다.
app.mount("/static", StaticFiles(directory="static"), name="static")


# =====================================================================
# 로그인
# =====================================================================
# 비밀번호는 .env 의 APP_PASSWORD 값입니다. 바꾸려면 .env 만 고치면 됩니다.
# 비워두면 로그인 기능 자체가 꺼집니다(집 안 테스트용).
APP_PASSWORD = os.getenv("APP_PASSWORD", "")

# 쿠키 위조 방지용 서버 비밀값.
# .env 에 SECRET_KEY 가 없으면 서버 재시작마다 새로 생기고, 그때 다시 로그인해야 합니다.
SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_hex(32)

COOKIE_NAME = "lawfinder_auth"
COOKIE_DAYS = 30
PUBLIC_PATHS = {"/login", "/favicon.ico"}


def _make_token() -> str:
    """비밀번호를 서버 비밀값으로 서명한 값. 쿠키에는 이 값이 담깁니다."""
    return hmac.new(SECRET_KEY.encode(), APP_PASSWORD.encode(), hashlib.sha256).hexdigest()


def _is_logged_in(request: Request) -> bool:
    if not APP_PASSWORD:
        return True
    token = request.cookies.get(COOKIE_NAME, "")
    # compare_digest 는 타이밍 공격을 막습니다. == 대신 이걸 쓰는 게 정석입니다.
    return hmac.compare_digest(token, _make_token())


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """
    모든 요청이 여기를 먼저 지나갑니다.
    ASP.NET Core 의 미들웨어 파이프라인과 같은 개념입니다.
    """
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static"):
        return await call_next(request)
    if _is_logged_in(request):
        return await call_next(request)
    if path.startswith("/api"):
        return JSONResponse({"error": "로그인이 필요합니다."}, status_code=401)
    return RedirectResponse("/login", status_code=302)


LOGIN_HTML = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>로그인 · 법령 조회 도우미</title>
<style>
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#f7f7f4;color:#1a1c20;
       font-family:-apple-system,"Segoe UI","Malgun Gothic",sans-serif}
  form{background:#fffffc;border:1px solid #d6d3c9;border-radius:3px;
       padding:30px 28px;width:320px}
  h1{font-size:16px;margin:0 0 4px}
  p{font-size:12.5px;color:#6f6d64;margin:0 0 20px}
  input{width:100%;padding:10px 12px;border:1px solid #d6d3c9;border-radius:3px;
        font:inherit;box-sizing:border-box}
  input:focus{outline:2px solid #2c4a52;outline-offset:1px;border-color:transparent}
  button{width:100%;margin-top:10px;padding:10px;border:none;border-radius:3px;
         background:#1a1c20;color:#fff;font:inherit;font-weight:600;cursor:pointer}
  button:hover{background:#000}
  .err{margin-top:12px;padding:9px 11px;border-left:3px solid #a8332c;
       background:#fdf5f4;font-size:12.5px;color:#6d2e2a}
</style></head>
<body>
<form method="post" action="/login">
  <h1>법령 조회 도우미</h1>
  <p>이용하려면 비밀번호를 입력하세요.</p>
  <input type="password" name="password" placeholder="비밀번호" autofocus required>
  <button type="submit">들어가기</button>
  __ERROR__
</form>
</body></html>"""


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _is_logged_in(request):
        return RedirectResponse("/", status_code=302)
    return LOGIN_HTML.replace("__ERROR__", "")


@app.post("/login")
def login_submit(password: str = Form(...)):
    # ★ 2026-08-19 — compare_digest 는 비ASCII 문자열을 받으면 TypeError 를 냅니다
    #   ("comparing strings with non-ASCII characters is not supported").
    #   한글 앱이라 사용자가 한글로 입력하거나 한글 비밀번호를 설정하면
    #   "비밀번호가 맞지 않습니다" 대신 500 이 났습니다. 바이트로 비교합니다.
    if APP_PASSWORD and hmac.compare_digest(
            str(password or "").encode("utf-8"), APP_PASSWORD.encode("utf-8")):
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie(
            COOKIE_NAME,
            _make_token(),
            max_age=COOKIE_DAYS * 24 * 3600,
            httponly=True,   # 자바스크립트에서 못 읽게 막습니다
            samesite="lax",
            secure=True,     # HTTPS 전용 쿠키. 로컬(http) 접속 시 로그인 안 됩니다
        )
        return resp
    err = '<div class="err">비밀번호가 맞지 않습니다.</div>'
    return HTMLResponse(LOGIN_HTML.replace("__ERROR__", err), status_code=401)


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# --- 요청 모델 -------------------------------------------------------
# C# 의 DTO 클래스와 같습니다. 타입 검증을 pydantic 이 자동으로 해줍니다.
class AskRequest(BaseModel):
    question: str
    target: str = "auto"        # auto = 전 계층 자동 검색
    skip_clarify: bool = False  # true 면 되묻지 않고 바로 검색
    answered: str = ""          # 이전 라운드에서 고른 조건
    round: int = 0              # 되묻기 라운드 (0부터)
    note: str = ""              # 사용자가 직접 적은 추가 설명
    laws: list[str] = []        # v1.33 — 사용자가 지정한 "참고할 법령" (선택, 최대 3개)
    laws_mode: str = "only"     # v1.33 — "only": 이 법령에서만 찾기 / "prefer": 우선 참고(다른 법령도 함께)


# =====================================================================
# 인용 검증
# =====================================================================
import re  # noqa: E402

# "제14조의2제1항제3호" 같은 패턴을 뽑습니다.
# "토양환경보전법 시행령 제3조제1항제4호" 처럼 법령명이 앞에 붙는 경우가 많습니다.
# 법률과 시행령은 조문 번호가 겹치므로(둘 다 제3조가 있음) 법령명을 같이 봐야 합니다.
CITE_RE = re.compile(
    # ★ 「」와 [] 를 넣어야 합니다.
    #   답변은 법령명을 "[토양환경보전법 시행규칙] 제12조제2항" 처럼 씁니다.
    #   괄호를 빼두면 "]" 에서 끊겨 법령명이 통째로 안 잡히고, 그러면
    #   아래 _guess_law 가 default(법률)로 떨어뜨립니다.
    #   실제로 시행규칙 제12조제2항 인용이 법률 제12조제2항으로 표시됐습니다.
    r"(?:([가-힣A-Za-z0-9·ㆍ\s「」\[\]]{2,40}?)\s*)?"              # 앞에 붙은 법령명(선택)
    r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?"                          # 제○조(의○)
    r"(?:\s*제\s*(\d+)\s*항)?(?:\s*제\s*(\d+)\s*호)?"            # 제○항 제○호
)

# 법령명 뒤에 붙는 단계 표시. 긴 것부터 확인해야 "시행규칙"이 "규칙"으로 잘리지 않습니다.
_SUFFIX = ["시행규칙", "시행령"]


def _norm(name: str) -> str:
    # 괄호류는 이름의 일부가 아닙니다. 떼고 비교합니다.
    return re.sub(r"[\s「」『』\[\]()]+", "", name or "")


def _named_law(prefix: str, laws: list[dict]) -> str:
    """접두사에 법령 '이름' 이 통째로 들어 있으면 그 이름을 돌려줍니다."""
    p = _norm(prefix)
    if not p:
        return ""
    for law in sorted(laws, key=lambda x: -len(x.get("name", ""))):
        nm = _norm(law.get("name", ""))
        if nm and p.endswith(nm):
            return law["name"]
    return ""


def _guess_law(prefix: str, laws: list[dict], default: str) -> str:
    """
    인용문 앞에 붙은 글자에서 법령명을 추려냅니다.

    "…규정합니다. 토양환경보전법 시행령" 처럼 앞 문장이 섞여 오므로
    실제 수집한 법령 이름과 뒤에서부터 대조합니다.
    """
    p = _norm(prefix)
    if not p:
        return default
    # 이름이 긴 것부터 맞춰야 "시행령"이 "법률"에 먼저 걸리지 않습니다.
    for law in sorted(laws, key=lambda x: -len(x.get("name", ""))):
        nm = _norm(law.get("name", ""))
        if nm and p.endswith(nm):
            return law["name"]
    # "시행령 제3조" 처럼 법령명 없이 단계만 쓴 경우
    # ★ 2026-08-19 — default(= 직전에 이름이 명시된 법령)가 이미 하위법령이면
    #   여기서 단계 이름을 **덧붙이기만** 해서 "…시행규칙시행령" 이라는 없는
    #   이름을 찾다가 실패하고, 결국 default(시행규칙)를 그대로 돌려줬습니다.
    #   실측: 근거에 「…시행규칙」 제12조제2항 을 쓴 뒤 설명에서
    #        "시행령 제8조에서 정합니다" → 시행규칙 제8조(검사기관)가 근거로 붙음.
    #   아래 축약형("영 제8조") 경로는 이미 접미사를 떼고 있었는데, 정작
    #   풀어 쓴 형태가 더 나빴습니다. 같은 방식으로 떼어냅니다.
    for suf in _SUFFIX:
        if p.endswith(suf):
            base = _norm(default)
            for s2 in _SUFFIX:
                if base.endswith(s2):
                    base = base[: -len(s2)]
                    break
            for law in laws:
                if _norm(law.get("name", "")) == base + suf:
                    return law["name"]

    # ── 법령 문언의 축약형 ──────────────────────────────────
    # 법령은 자기들끼리 줄여 부릅니다.
    #   시행령 → "영",  시행규칙 → "규칙",  상위 법률 → "법"
    # 별표 4 의 "영 제8조제1항제2호" 는 시행령 제8조입니다.
    # 이것을 법률 제8조로 잡으면 "타인 토지에의 출입 등" 이라는
    # 전혀 다른 조문이 근거로 붙습니다. 실제로 그렇게 표시됐습니다.
    #
    # ★ 앞 글자가 한글이면 낱말의 일부입니다. "토양환경보전법" 의 "법" 을
    #   축약형으로 오인하면 안 되므로 앞이 한글이 아닐 때만 인정합니다.
    #
    # ★★ 반드시 **공백을 지우지 않은 원본** 으로 봐야 합니다.
    #    _norm() 은 공백을 없애므로 "따른 영" 이 "따른영" 이 되어,
    #    "영" 앞이 한글이 되어버려 축약형으로 인정되지 않습니다.
    #    실제로 "해당하면 영 제8조제1항제1호" 가 법률 제8조(타인 토지에의 출입)로
    #    잘못 붙었습니다. 문장 안에 있는 인용은 대부분 이 꼴입니다.
    m = re.search(r"(?:^|[^가-힣])(영|규칙|법)\s*$", (prefix or "").strip())
    if m:
        base = _norm(default)
        for suf in _SUFFIX:            # default 가 하위법령이면 법률 이름만 남깁니다
            if base.endswith(suf):
                base = base[: -len(suf)]
                break
        want = base + {"영": "시행령", "규칙": "시행규칙", "법": ""}[m.group(1)]
        for law in laws:
            if _norm(law.get("name", "")) == want:
                return law["name"]
    return default


_HANG_MARKS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮"


def extract_hang(body: str, hang: str = "", ho: str = "") -> str:
    """
    조문 본문에서 지정한 항(①②③)과 호(1. 2. 3.)만 잘라냅니다.

    근거 조문 카드에 "제8조제2항제4호" 를 표시할 때 조문 전체를 보여주면
    항목마다 같은 내용이 반복되어 쓸모가 없습니다.
    항 번호가 범위를 벗어나거나 못 찾으면 원문을 그대로 돌려줍니다.
    """
    if not body:
        return ""

    text = body
    # 항 추출
    if hang:
        try:
            n = int(hang)
        except ValueError:
            n = 0
        if 1 <= n <= len(_HANG_MARKS):
            mark = _HANG_MARKS[n - 1]
            nxt = _HANG_MARKS[n] if n < len(_HANG_MARKS) else None
            i = text.find(mark)
            if i >= 0:
                j = text.find(nxt, i) if nxt else -1
                text = text[i:j] if j > i else text[i:]

    # 호 추출 — 항 안에서 "4." 부터 "5." 직전까지
    if ho:
        pat = re.compile(r"(?:^|\s)" + re.escape(ho) + r"\.\s")
        mt = pat.search(text)
        if mt:
            start = mt.start()
            try:
                nxt_no = str(int(ho) + 1)
                pat2 = re.compile(r"(?:^|\s)" + nxt_no + r"\.\s")
                mt2 = pat2.search(text, mt.end())
                text = text[start:mt2.start()] if mt2 else text[start:]
            except ValueError:
                text = text[start:]

    return text.strip()


# ── 별표 경고 판정 ──────────────────────────────────────────────
# 별표 이름이 나왔다고 다 경고할 일이 아닙니다. 경고해야 하는 것은
# **본문을 못 받은 별표의 수치를 답변이 말한 경우** 뿐입니다.
_BP_RE = re.compile(r"(별표|별지)\s*제?\s*(\d+)")

# 수치 — 이게 없으면 담당자가 잘못 안내할 숫자 자체가 없습니다.
# ★ 2026-08-19 — 한 글자 단위(원·일·분·배·주…)는 **다른 낱말의 첫 글자**이기도
#   합니다. 예전 패턴은 "별표 4 원문이" 의 `4 원` 을 금액으로 읽어서, 수치가
#   전혀 없는 부정문에까지 경고를 띄웠습니다.
#   → 여러 글자 단위는 그대로 두고, 한 글자 단위는 **뒤가 한글이 아니거나
#     조사·수식어로 이어질 때만** 단위로 인정합니다.
_NUM_RE = re.compile(
    r"\d[\d,\.]*\s*(?:"
    r"제곱미터|킬로그램|퍼센트|주일|개월|시간|리터|만원|킬로|그램|미터|%"
    r"|(?:년|월|일|주|분|초|회|차|건|명|배|톤|원)"
    r"(?:(?![가-힣])"
    r"|(?=마다|이내|이상|이하|미만|초과|간|째|씩|동안|부터|까지)"
    r"|(?=[입이은는을를에의과와로만도및]))"       # 조사·서술격 조사 (8년입니다, 30일에 …)
    r")")

# 부정·유보 문맥. 이런 문장은 "근거로 썼다" 가 아니라
# "근거가 없어서 말할 수 없다" 는 **경고와 같은 편** 의 문장입니다.
_BP_NEGATIVE = (
    "제공되지", "제공되어", "확인되지", "확인할 수 없", "확인이 어렵",
    "확정할 수 없", "판단할 수 없", "알 수 없", "가져오지 못", "받지 못",
    "포함되어 있지", "포함되지", "없으므로", "없어서", "없기 때문",
    "직접 확인", "원문이 없", "원문을 확인",
)


def _bp_used_as_basis(text: str) -> set:
    """
    답변에서 **근거로 쓰인** 별표·별지 번호를 (구분, 번호) 집합으로 돌려줍니다.

    ★ 2026-08-18 — 예전에는 본문 전체에서 "별표 4" 를 찾기만 하면 경고를
      띄웠습니다. 그런데 모델이 우리가 시킨 대로

          "「…관리지침」 별표 4 원문이 제공되지 않아 정기검사 주기를
           확정할 수 없습니다."

      라고 **부정문으로** 썼는데도 경고가 떴습니다. 답이 틀린 것처럼 보여서
      담당자가 멀쩡한 답변을 못 믿게 됩니다. (실제 2026-08-18 테스트)

    판정을 문장 단위로 바꿉니다. 별표가 나온 문장이
      · 부정·유보 문맥이면            → 경고 대상 아님 (오히려 잘 쓴 문장)
      · 수치가 하나도 없으면          → 경고 대상 아님 (틀릴 숫자가 없음)
    나머지, 즉 "별표 4에 따라 매 8년" 처럼 **번호와 수치가 같은 문장에**
    있을 때만 경고합니다.
    """
    out = set()
    # 줄바꿈과 문장 끝(다./요./음.)으로 자릅니다.
    sents = re.split(r"\n|(?<=다\.)\s|(?<=요\.)\s|(?<=음\.)\s", str(text or ""))
    for i, sent in enumerate(sents):
        found = _BP_RE.findall(sent)
        if not found:
            continue

        # ★ 2026-08-19 — 순서를 뒤집었습니다. 예전에는 부정어가 하나라도 있으면
        #   무조건 넘어갔는데, 우리가 프롬프트로 "원문을 확인해야 한다" 고
        #   시켜 놓았기 때문에 모델이 **수치와 유보를 한 문장에** 같이 씁니다.
        #     "별표 4에 따라 5년마다 실시하나, 정확한 주기는 원문을 확인하십시오."
        #   이러면 경고가 안 뜨는데, 정작 위험한 "5년" 은 화면에 남습니다.
        #   → 수치가 있으면 유보 문구가 있어도 경고합니다.
        #     유보만 있고 수치가 없을 때만 넘어갑니다.
        if _NUM_RE.search(sent):
            out.update(found)
            continue
        if any(w in sent for w in _BP_NEGATIVE):
            continue

        # 수치가 다음 문장으로 넘어간 경우도 봅니다.
        #   "검사주기는 별표 4에서 정합니다. 그 주기는 8년입니다."
        nxt = sents[i + 1] if i + 1 < len(sents) else ""
        if nxt and not _BP_RE.search(nxt) and _NUM_RE.search(nxt) \
                and not any(w in nxt for w in _BP_NEGATIVE):
            out.update(found)
    return out


# ── 【계산】 블록 검산 ────────────────────────────────────────────
# ★ 2026-08-19 실사용 사고 —
#   답변이 이렇게 나왔습니다.
#       기준일: 2010년 9월 23일   + 주기: 8년   = 다음 검사: 2038년 9월 23일
#   2010 + 8 = 2018 입니다. **20년이 틀렸습니다.**
#   게다가 사용자는 "설치 15년 경과" 라고만 했지 날짜를 준 적이 없습니다.
#   2010년 9월 23일은 모델이 지어낸 날짜입니다.
#
#   산수는 프롬프트로 고쳐지지 않습니다. 로컬 모델에게 연도 덧셈을 정확히
#   시키는 것보다, **나온 답을 코드가 검산**하는 편이 확실합니다.
_CALC_BLOCK_RE = re.compile(r"【계산】(.*?)(?=【|\Z)", re.S)
_CALC_DATE_RE = re.compile(r"(\d{4})\s*[년\-\.\/]\s*(\d{1,2})\s*[월\-\.\/]\s*(\d{1,2})\s*일?")
_CALC_PERIOD_RE = re.compile(r"(\d+)\s*(년|개월|달|주|일)")


def _add_period(y: int, m: int, d: int, n: int, unit: str) -> tuple:
    """기준일에 주기를 더합니다. 2월 29일은 말일로 눕힙니다."""
    import calendar
    if unit == "년":
        y += n
    elif unit in ("개월", "달"):
        t = (m - 1) + n
        y += t // 12
        m = t % 12 + 1
    else:
        from datetime import date, timedelta
        days = n * 7 if unit == "주" else n
        try:
            t = date(y, m, d) + timedelta(days=days)
            return t.year, t.month, t.day
        except ValueError:
            return y, m, d
    d = min(d, calendar.monthrange(y, m)[1])
    return y, m, d


def verify_calc(answer_text: str, user_text: str) -> list[str]:
    """
    답변의 【계산】 블록을 검산합니다. 문제가 있으면 경고 문구를 돌려줍니다.

    두 가지를 봅니다.
      (1) 기준일 + 주기 = 결과 가 실제로 맞는지
      (2) 기준일이 **사용자가 준 정보 안에 있는 날짜**인지
          (없으면 모델이 지어낸 것입니다 — 이게 제일 위험합니다)
    """
    out: list[str] = []
    mb = _CALC_BLOCK_RE.search(answer_text or "")
    if not mb:
        return out
    block = mb.group(1)

    dates = _CALC_DATE_RE.findall(block)
    # ★ 주기를 찾을 때는 **날짜를 먼저 지웁니다.** 안 그러면 "2010년 9월" 의
    #   "2010년" 이 주기로 잡혀 "+ 2010년" 이라는 엉뚱한 경고가 나갑니다.
    periods = [(n, u) for n, u in _CALC_PERIOD_RE.findall(_CALC_DATE_RE.sub(" ", block))
               if len(n) <= 3]
    if len(dates) < 2 or not periods:
        return out                               # 계산 형태가 아니면 넘어갑니다

    try:
        by, bm, bd = (int(x) for x in dates[0])
        ry, rm, rd = (int(x) for x in dates[-1])
    except ValueError:
        return out

    # ★ 2026-10-02 — 기준일이 "설치일 + 10년" 처럼 한 번 더 계산된 날이고,
    #   결과도 "+ 8년 이후 90일" 처럼 여러 단계를 거칩니다. 주기 하나만
    #   더해 보면 맞는 계산도 틀렸다고 경고했습니다. 블록에 적힌 주기들의
    #   **조합**(순서대로 일부를 골라 더한 것)까지 맞춰 봅니다.
    #   2010 + 8 = 2038 같은 산수 실수는 어느 조합으로도 안 나오므로 여전히 잡힙니다.
    plist = []
    for n_s, unit in periods[:6]:
        try:
            plist.append((int(n_s), unit))
        except ValueError:
            pass

    def _reachable(y, m, d):
        """(y,m,d) 에 plist 의 부분집합을 순서대로 더해 나올 수 있는 날짜들."""
        seen = set()
        for mask in range(1, 1 << len(plist)):
            t = (y, m, d)
            for i, (n, u) in enumerate(plist):
                if mask >> i & 1:
                    t = _add_period(*t, n, u)
            seen.add(t)
        return seen

    # (1) 산수 검산 — 블록 안 어떤 날짜에서 출발해 주기 조합으로 결과가 나오면 통과
    ok = False
    for ds in dates[:-1]:
        try:
            start = tuple(int(x) for x in ds)
        except ValueError:
            continue
        if (ry, rm, rd) in _reachable(*start):
            ok = True
            break
    if not ok:
        # 경고 문구에 쓸 주기는 "주기" 라고 적힌 줄의 것을 우선합니다.
        # (그냥 첫 번째를 쓰면 "설치 후 10년 경과일" 의 10년을 집습니다)
        pick = periods[0]
        for line in block.splitlines():
            if "주기" in line:
                m2 = _CALC_PERIOD_RE.findall(_CALC_DATE_RE.sub(" ", line))
                m2 = [(n, u) for n, u in m2 if len(n) <= 3]
                if m2:
                    pick = m2[0]
                    break
        n_s, unit = pick
        ey, em, ed = _add_period(by, bm, bd, int(n_s), unit)
        out.append(
            f"답변의 계산이 맞지 않습니다. "
            f"{by}년 {bm}월 {bd}일 + {n_s}{unit} 은 {ey}년 {em}월 {ed}일 인데 "
            f"답변은 {ry}년 {rm}월 {rd}일 이라고 적었습니다. "
            f"날짜를 그대로 쓰지 마시고 직접 확인하세요."
        )

    # (2) 기준일이 사용자가 준 날짜인지
    src = re.sub(r"[^\d]", "", user_text or "")
    stamp = f"{by:04d}{bm:02d}{bd:02d}"
    loose = f"{by:04d}"
    # ★ 2026-10-02 — 기준일이 사용자가 준 날짜에서 계산된 날(설치일 + 10년)이면 정상입니다.
    derived = False
    for ds in _CALC_DATE_RE.findall(user_text or ""):
        try:
            u = tuple(int(x) for x in ds)
        except ValueError:
            continue
        if (by, bm, bd) in _reachable(*u):
            derived = True
            break
    if stamp not in src and loose not in src and not derived:
        out.append(
            f"답변이 기준일을 {by}년 {bm}월 {bd}일 로 잡았지만, "
            f"질문·조건 어디에도 그 날짜가 없습니다. AI 가 지어낸 날짜입니다. "
            f"실제 시설 설치일·직전 검사일을 확인해 다시 계산하세요."
        )
    return out


def verify_citations(answer_text: str, laws: list[dict]) -> list[dict]:
    """
    AI 답변에 나온 조문 번호가 실제로 존재하는지 대조합니다.

    ★ 조문 번호만으로 찾으면 안 됩니다.
      법률 제3조와 시행령 제3조가 둘 다 있으므로, 인용문 앞의 법령명까지 봐야
      "시행령 제3조"를 법률 제3조로 오인하지 않습니다.
    """
    # (법령명, 조문번호, 가지번호) -> (제목, 본문, 계층)
    index = {}
    for law in laws:
        for art in law.get("articles", []):
            key = (law.get("name", ""), art.get("조문번호", ""), art.get("조문가지번호", ""))
            if key not in index:
                index[key] = (art.get("조문제목", ""), art.get("조문내용", ""),
                              law.get("level") or law.get("kind", ""))

    # 기본 법령 = 가장 상위(법률). 법령명 없이 "제14조"만 쓴 경우에 씁니다.
    base_law = ""
    for lv in ("법률", "법령"):
        for law in laws:
            if (law.get("level") or law.get("kind")) == lv:
                base_law = law["name"]
                break
        if base_law:
            break
    if not base_law and laws:
        base_law = laws[0].get("name", "")

    seen = set()
    out = []
    # ★ 법령명 없이 "(제12조제2항)" 만 쓴 인용의 기준.
    #   답변은 【근거】에서 「…시행규칙」 제12조제2항 처럼 이름을 밝히고,
    #   【설명】에서는 문장 끝에 (제12조제2항) 만 붙입니다.
    #   기본값(법률)으로 떨어뜨리면 시행규칙 제12조가 법률 제12조로 붙습니다.
    #   (실제로 "신고수리 여부 통지" 조문이 검사주기 근거로 표시됐습니다)
    #   그래서 **마지막으로 이름이 명시된 법령**을 기준으로 삼습니다.
    #   축약형("영"·"규칙")은 기준을 바꾸지 않습니다. 그때그때 가리키는 것이라
    #   기준으로 삼으면 뒤따르는 인용이 줄줄이 끌려갑니다.
    ctx_law = ""
    for prefix, jo, gaji, hang, ho in CITE_RE.findall(answer_text):
        gaji = gaji or ""
        named = _named_law(prefix, laws)
        if named:
            ctx_law = named
        law_name = _guess_law(prefix, laws, ctx_law or base_law)

        label = f"제{jo}조" + (f"의{gaji}" if gaji else "")
        if hang:
            label += f"제{hang}항"
        if ho:
            label += f"제{ho}호"

        # 답변의 【근거】 블록과 【설명】에서 같은 조문이 두 번 인용되므로
        # 법령명·조·가지·항·호를 모두 합친 키로 중복을 걸러냅니다.
        key = (_norm(law_name), jo, gaji, hang or "", ho or "")
        if key in seen:
            continue
        seen.add(key)

        # AI 가 "시행령"/"시행규칙" 이라고 적었는데 실제로 찾은 법령이
        # 그 단계가 아니면, 조문을 잘못 짚은 것입니다. 화면에 경고를 띄웁니다.
        want = ""
        for suf in _SUFFIX:
            if _norm(prefix).endswith(suf):
                want = suf
                break

        hit = index.get((law_name, jo, gaji))
        mismatch = ""
        if want and not _norm(law_name).endswith(want):
            mismatch = (f"답변은 '{want}' 이라고 했으나 실제로는 "
                        f"'{law_name}' 의 조문입니다 — 원문을 반드시 확인하세요")
        if hit is None:
            # 법령명 추정이 빗나갔을 수 있으니, 번호만으로 한 번 더 찾아봅니다.
            for (nm, j, g), v in index.items():
                if (j, g) == (jo, gaji):
                    if _norm(nm) != _norm(law_name):
                        # AI 가 적은 법령명과 실제 조문이 있는 법령이 다릅니다.
                        mismatch = f"답변은 '{law_name}' 이라고 했으나 실제로는 '{nm}' 조문입니다"
                    law_name, hit = nm, v
                    break

        out.append(
            {
                "jo": jo, "gaji": gaji,
                "label": label,
                "ok": hit is not None,
                "law": law_name if hit else "",
                "title": hit[0] if hit else "",
                # 인용한 항·호만 잘라 보여줍니다. 조문 전체를 넣으면
                # 제2항제4호와 제2항제5호가 똑같은 내용으로 보입니다.
                "text": (extract_hang(hit[1], hang, ho)[:1200] if hit else ""),
                "full": (hit[1][:4000] if hit else ""),
                "level": hit[2] if hit else "",
                "mismatch": mismatch,
            }
        )
    return out


# ── 되묻기 라운드 사이 재사용 캐시 ────────────────────────────
# 되묻기는 조문을 확보한 뒤에 하므로(v1.7), 라운드가 넘어갈 때마다
# 용어 변환과 법제처 조회가 처음부터 다시 실행되고 있었습니다.
# 질문과 검색 대상이 같으면 그 결과를 재사용합니다.
_SEARCH_CACHE: dict = {}
_CACHE_TTL = 600.0          # 초. 이보다 오래된 것은 버립니다.


def _cache_get(key):
    import time
    hit = _SEARCH_CACHE.get(key)
    if not hit:
        return None
    if time.time() - hit[0] > _CACHE_TTL:
        _SEARCH_CACHE.pop(key, None)
        return None
    return hit[1]


def _cache_put(key, value):
    import time
    # 오래된 항목을 정리해 무한정 쌓이지 않게 합니다.
    now = time.time()
    for k in [k for k, v in _SEARCH_CACHE.items() if now - v[0] > _CACHE_TTL]:
        _SEARCH_CACHE.pop(k, None)
    _SEARCH_CACHE[key] = (now, value)


# ── 부정 조건 ────────────────────────────────────────────────
# 되묻기에서 "아니오" 로 답한 항목은 검색 범위를 좁히는 중요한 조건입니다.
# 자연어로 프롬프트에 넣기만 하면 검색 단계에는 반영되지 않아,
# "방사성폐기물이 아니다" 라고 답했는데 방사성폐기물관리법이 조회되는 일이 생깁니다.
_NO_ANSWER_RE = re.compile(
    r"([^/]{2,60}?)\s*[:：]\s*(아니오|아니요|아니다|해당없음|해당하지\s*않음|없음|아님)")


def _excluded_terms(answered: str) -> list:
    """
    "…에 해당하나요?: 아니오" 형태에서 제외할 주제어를 뽑습니다.
    예) "방사성폐기물에 해당하나요?: 아니오"  ->  ["방사성폐기물"]
    """
    out = []
    for q, _ in _NO_ANSWER_RE.findall(answered or ""):
        # 질문에서 핵심 명사만 남깁니다.
        t = re.sub(r"(에\s*해당하나요|에\s*해당합니까|인가요|입니까|맞나요|있나요|"
                   r"하나요|받았나요|였나요)\s*\??$", "", q.strip())
        # "배출하는 폐기물이 방사성폐기물" 처럼 앞말이 붙으면 마지막 명사구만 씁니다.
        t = t.split()[-1] if t.split() else ""
        t = re.sub(r"(를|을|이|가|은|는|의)$", "", t).strip()
        if len(t) >= 2:
            out.append(t)
    return out


def _norm_q(q: str) -> str:
    """되묻기 질문 텍스트 비교용 정규화. 공백·물음표 차이는 같은 질문으로 봅니다."""
    return re.sub(r"\s+", "", str(q or "")).rstrip("?？")


def _asked_questions(answered: str) -> set:
    """
    answered 문자열("질문: 답변 / 질문: 답변 / …")에서 질문 텍스트만
    정규화해 집합으로 돌려줍니다. 이미 물은 질문을 다시 걸러내는 데 씁니다.

    ★ 로컬 모델은 프롬프트의 "이미 답변된 조건은 다시 묻지 마라" 지시를
      가끔 무시하고 직전 라운드와 완전히 같은 질문을 그대로 다시 냅니다
      (실사례: 4개 질문이 "모름"으로 답해도 3라운드 연속 토씨 하나 안 틀리고
      반복됨). 프롬프트만 믿지 않고 코드에서 한 번 더 걸러냅니다.
    """
    out = set()
    for seg in (answered or "").split(" / "):
        seg = seg.strip()
        if not seg:
            continue
        q = re.split(r"[:：]", seg, 1)[0].strip()
        if q:
            out.add(_norm_q(q))
    return out


def _is_repeat_question(q: str, already: set) -> bool:
    """
    이번 질문이 이미 물은 것인지 봅니다.

    ★ 2026-08-19 — 예전에는 질문 **전체**만 비교했습니다. 그런데 모델이
      이미 답변된 조건을 통째로 베껴 이렇게 냅니다.

          질문: "누출검사 대상인지 확인이 필요합니다.: 지하매설 저장시설"
          이미 물은 것: "누출검사 대상인지 확인이 필요합니다."

      뒤에 답이 붙어 있어서 문자열이 달라지고, 중복 판정을 빠져나가
      **같은 질문을 계속 다시 물었습니다.** (실사용 2/6 라운드에서 발생)
      → 콜론 앞부분끼리도 비교하고, 한쪽이 다른 쪽으로 시작하면
        같은 질문으로 봅니다.
    """
    n = _norm_q(q)
    if not n:
        return True
    if n in already:
        return True
    head = _norm_q(re.split(r"[:：]", str(q or ""), 1)[0])
    if head and head in already:
        return True
    for a in already:
        if len(a) >= 8 and (n.startswith(a) or a.startswith(n)):
            return True
    # ★ 2026-09-29 — 표현만 조금 바꿔 다시 묻는 경우가 3~4라운드 이어졌습니다.
    #     "누출검사를 언제 받았습니까?" → "마지막 누출검사를 언제 받았습니까?"
    #     → "주유소 지하 저장시설 누출검사를 언제 받았습니까?"
    #     "시설 규모는 어떻게 됩니까" → "세차장 규모는 어떻게 됩니까"
    #   한쪽이 다른 쪽을 통째로 품거나, 글자 유사도가 0.72 이상이면 같은 질문으로 봅니다.
    #   (실측: 서로 다른 질문끼리는 0.3~0.65, 표현만 바꾼 질문은 0.78 이상)
    from difflib import SequenceMatcher
    for a in already:
        if len(a) >= 6 and len(n) >= 6:
            if a in n or n in a:
                return True
            if SequenceMatcher(None, a, n).ratio() >= 0.72:
                return True
    return False


TARGET_LABEL = {"law": "법령", "eflaw": "법령", "admrul": "행정규칙",
                "ordin": "자치법규", "expc": "법령해석례"}


def _err(msg: str):
    """
    앱 오류 응답.

    ★ 상태 코드를 200 으로 둡니다.
      Cloudflare 등 프록시가 5xx 응답 본문을 자체 HTML 오류 페이지로 교체해버려,
      실제 오류 메시지가 사용자에게 전달되지 않기 때문입니다.
      화면은 상태 코드가 아니라 error 필드를 보고 판단합니다.
    """
    return JSONResponse({"error": msg}, status_code=200)


class PdfRequest(BaseModel):
    """PDF 로 만들 조회 결과. 화면이 갖고 있는 내용을 그대로 보냅니다."""
    question: str = ""
    answered: str = ""
    answer: str = ""
    citations: list = []
    laws: list = []


@app.post("/api/pdf")
def api_pdf(req: PdfRequest):
    """
    조회 결과를 PDF 로 만들어 내려줍니다.

    브라우저 인쇄를 쓰지 않는 이유:
      인쇄 대화상자의 기본 대상이 "Microsoft Print to PDF" 인데,
      이것으로 저장하면 화면이 이미지로 렌더링되어 텍스트 복사가 안 됩니다.
      사용자가 매번 대상을 바꾸게 할 수 없으므로 서버에서 직접 만듭니다.
    """
    from fastapi.responses import Response
    try:
        data = pdf_maker.build(req.model_dump())
    except RuntimeError as e:          # 폰트 없음
        return _err(str(e))
    except Exception as e:
        return _err(f"PDF 생성 실패: {e}")

    name = f"법령조회_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
    quoted = urllib.parse.quote(name)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
    )


@app.get("/api/version")
def api_version():
    """화면·서버 버전과 로컬 LLM 정보. (watch_and_test.py 가 서버 확인용으로도 부릅니다)

    ★ 2026-09-29 — 예전에는 클라우드 API 키 상태(키 끝 6자리 포함)를 돌려줬습니다.
      로그인을 없앤 뒤로 누구나 볼 수 있어서 뺐습니다.
      이 주소는 자주 불리므로 llama-server 에 새로 묻지 않고 알고 있는 값만 씁니다.
    """
    return {
        "version": VERSION,
        "llm": {
            "server": ai_client.LOCAL_SERVER,
            "model": ai_client.LOCAL_MODEL,
            "ctx": ai_client._ctx_cache.get("n") or ai_client.LOCAL_CTX_FALLBACK,
        },
    }


# --- 화면 -----------------------------------------------------------
@app.get("/")
def index():
    return FileResponse("static/index.html")


# --- API ------------------------------------------------------------
@app.get("/api/search")
def api_search(q: str, target: str = "law"):
    """법령명으로 목록 검색."""
    try:
        rows = law_client.search(target, q)
    except law_client.LawApiError as e:
        return _err(str(e))

    # 태그 이름이 확실치 않으므로 pick() 으로 후보를 여러 개 시도합니다.
    items = []
    for r in rows:
        items.append(
            {
                "id": law_client.pick(r, "법령ID", "행정규칙ID", "자치법규ID", "ID"),
                "name": law_client.pick(r, "법령명_한글", "법령명한글", "행정규칙명", "자치법규명"),
                "kind": law_client.pick(r, "법령구분명", "행정규칙종류", "자치법규종류"),
                "ministry": law_client.pick(r, "소관부처명", "소관부처", "지자체기관명"),
                "enforced": law_client.pick(r, "시행일자"),
                "promulgated": law_client.pick(r, "공포일자"),
                "promulgation_no": law_client.pick(r, "공포번호"),
                "_raw": r,  # 태그 확인용. 확정되면 지우세요.
            }
        )
    return {"count": len(items), "items": items}


@app.get("/api/detail")
def api_detail(id: str, target: str = "law"):
    """법령 본문(조문) 조회."""
    try:
        data = law_client.get_detail(target, id)
    except law_client.LawApiError as e:
        return _err(str(e))
    return data


@app.get("/api/raw", response_class=PlainTextResponse)
def api_raw(request: Request, target: str, value: str, mode: str = "search"):
    """
    원본 XML 그대로 보기.

    태그 이름을 확인할 때 쓰세요.
    예) /api/raw?target=law&value=토양환경보전법&mode=search

    ★ target·value·mode 를 뺀 나머지 쿼리스트링은 법제처로 그대로 넘어갑니다.
      문서에 없는 파라미터 이름을 시험해 볼 때 쓰세요.
      예) /api/raw?target=licbyl&value=토양환경보전법 시행규칙&search=2
          /api/raw?target=licbyl&value=x&MST=281911
    """
    extra = {k: v for k, v in request.query_params.items()
             if k not in ("target", "value", "mode")}
    try:
        return law_client.dump_raw(target, value, mode, extra)
    except law_client.LawApiError as e:
        return PlainTextResponse(str(e), status_code=200)


@app.post("/api/ask")
async def api_ask(req: AskRequest):
    """
    조회 결과를 NDJSON 스트림으로 흘려보냅니다.

    ★ 한 번에 응답하면 Cloudflare 가 100초에서 연결을 끊습니다(524).
      로컬 모델은 그보다 오래 걸리므로, 진행 상황을 계속 내보내
      연결을 살려둡니다. 화면에는 어느 단계인지 실시간으로 표시됩니다.
    """
    from fastapi.responses import StreamingResponse

    progress = _LoggedSteps()    # _ask_sync 가 단계마다 채웁니다 (서버 로그에도 자동 기록)

    async def gen():
        loop = asyncio.get_running_loop()
        task = loop.run_in_executor(None, _ask_logged, req, progress)
        sent = 0
        idle = 0
        while not task.done():
            await asyncio.sleep(1.0)
            if sent < len(progress):
                while sent < len(progress):      # 새로 생긴 단계를 흘려보냄
                    yield json.dumps({"progress": progress[sent]},
                                     ensure_ascii=False) + "\n"
                    sent += 1
                idle = 0
            else:
                idle += 1
                if idle >= 5:                    # 5초간 진전이 없으면 신호만 보냄
                    idle = 0
                    yield '{"ping":1}' + "\n"
        try:
            result = await task
        except Exception as e:                    # noqa: BLE001
            yield json.dumps({"error": f"처리 중 오류: {e}"},
                             ensure_ascii=False) + "\n"
            return
        while sent < len(progress):
            yield json.dumps({"progress": progress[sent]}, ensure_ascii=False) + "\n"
            sent += 1

        # ★ 2026-08-19 — _ask_sync 는 오류 경로에서 JSONResponse **객체**를
        #   돌려줍니다(_err). 이 엔드포인트는 예전에는 그것을
        #   그대로 반환했지만 지금은 NDJSON 으로 직렬화하므로,
        #   "TypeError: Object of type JSONResponse is not JSON serializable" 가
        #   여기서 터집니다. 그것도 try 밖이라 스트림이 중간에 끊기고, 화면에는
        #   원인 대신 "서버 응답이 중간에 끊겼습니다" 만 뜹니다.
        #   → 로컬 LLM 이 죽었거나 한도를 넘긴 **모든 경우**가 "연결 끊김" 으로
        #     보였고, 한도 팝업은 한 번도 뜰 수 없었습니다.
        if isinstance(result, JSONResponse):
            try:
                result = json.loads(bytes(result.body).decode("utf-8"))
            except Exception:                     # noqa: BLE001
                result = {"error": "처리 중 오류가 발생했습니다."}
            yield json.dumps(result, ensure_ascii=False) + "\n"
            return
        try:
            yield json.dumps({"result": result}, ensure_ascii=False) + "\n"
        except (TypeError, ValueError) as e:
            yield json.dumps({"error": f"결과를 보내지 못했습니다: {e}"},
                             ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


# ── 법령명 비교·체계도 검색 도우미 ─────────────────────────────
_DOTS_RE = re.compile(r"[·ㆍ・‧∙]")


def _law_key(name: str) -> str:
    """법령명 비교용 키. 공백·가운뎃점(· / ㆍ)·하위법령 꼬리(시행령/시행규칙)를 뗍니다."""
    n = re.sub(r"\s*(시행령|시행규칙)$", "", str(name or "").strip())
    return _DOTS_RE.sub("", n).replace(" ", "")


def _stmd_rows(name: str) -> list:
    """
    법령 체계도(lsStmd) 검색. 가운뎃점 표기가 달라 못 찾으면 바꿔서 다시 찾습니다.
    법제처 공식 이름은 "소음ㆍ진동관리법" 처럼 ㆍ(한글 아래아)를 쓰는데,
    AI 는 보통 ·(가운뎃점)으로 적습니다.
    """
    tried = []
    for q in (name, name.replace("·", "ㆍ"), name.replace("ㆍ", "·"), _DOTS_RE.sub("", name)):
        if not q or q in tried:
            continue
        tried.append(q)
        try:
            rows = law_client.search("lsStmd", q)
        except law_client.LawApiError:
            rows = []
        if rows:
            return rows
    return []


def _pick_stmd(rows: list, name: str):
    """
    체계도 검색 결과에서 그 법령에 해당하는 줄을 고릅니다.

    ★ 2026-09-29 — 체계도 검색은 **이름 부분일치 + 가나다순**입니다.
      "폐기물관리법" 으로 찾으면 "방사성폐기물 관리법"(ㅂ)이 "폐기물관리법"(ㅍ)보다
      앞에 옵니다. 예전에는 첫 줄을 그대로 써서, AI 가 법령명을 **맞게** 추측했는데도
      방사성폐기물 관리법 체계도 위에서 답했습니다. (음식점 폐식용유·사업장 폐기물
      질문이 방사성폐기물로 빠지던 사고의 진짜 원인)
      → 이름이 정확히 같은 줄 → 그 이름으로 끝나는 가장 짧은 줄 → 가장 짧은 줄.
    """
    key = _law_key(name)
    named = [(r, _law_key(law_client.row_name("lsStmd", r))) for r in rows]
    for r, k in named:
        if k == key:
            return r
    ends = [(r, k) for r, k in named if k.endswith(key)]
    pool = ends or named
    return min(pool, key=lambda x: len(x[1]))[0] if pool else None


def _ctx_key(f: dict, a: dict) -> str:
    """컨텍스트 조문의 표시 키 — "토양환경보전법 시행규칙 제12조", "… [별표 4] …" (평가용)."""
    no = str(a.get("조문번호", "") or "").strip()
    gaji = str(a.get("조문가지번호", "") or "").strip()
    if no:
        no = no.lstrip("0") or no
        g = gaji.lstrip("0")
        return f"{f.get('name', '')} 제{no}조" + (f"의{g}" if g else "")
    return f"{f.get('name', '')} {str(a.get('조문제목', ''))[:40]}".strip()


def _resolve_law_exact(cands: list):
    """
    법령명 후보(긴 것부터)를 법제처 검색 결과와 **이름이 정확히 같은** 법령으로 확정합니다.
    반환: (검색 결과 행, 정식 이름) 또는 None.
    공백·가운뎃점(·/ㆍ)만 무시하고 비교합니다. "시행령/시행규칙" 꼬리는 무시하지 않습니다
    (법률 제12조와 시행규칙 제12조는 다른 조문).
    """
    LAW_T = law_client.LAW_TARGET

    def key(n):
        return _DOTS_RE.sub("", str(n or "")).replace(" ", "")

    tried = set()
    for cand in cands:
        names = [cand]
        try:
            full = law_client.resolve_abbrev(cand)
            if full and full != cand:
                names.append(full)
        except Exception:                                   # noqa: BLE001
            pass
        for nm in names:
            for q in (nm, nm.replace("·", "ㆍ"), nm.replace("ㆍ", "·"), _DOTS_RE.sub("", nm)):
                if not q or q in tried:
                    continue
                tried.add(q)
                try:
                    rows = law_client.search(LAW_T, q, display=100)
                except law_client.LawApiError:
                    rows = []
                for r in rows:
                    rn = law_client.row_name(LAW_T, r)
                    if key(rn) == key(nm):
                        return r, rn
    return None


def _ask_article(req, steps, it):
    """
    ★ v1.31 — 조문 직접 조회 경로 ("폐기물관리법 제25조 알려줘").
    검색어 변환·체계도 수집·선별·되묻기를 모두 건너뛰고 그 조문 하나로 답합니다.
    법령을 못 찾으면 None (일반 경로로 넘어감). 법령은 찾았는데 조문이 없으면
    "없다" 고 분명히 답합니다 (지어내지 않음).
    """
    LAW_T = law_client.LAW_TARGET
    hit = _resolve_law_exact(it.law_candidates or [it.law])
    if not hit:
        return None
    row, name = hit
    rid = law_client.row_id(LAW_T, row)
    try:
        detail = law_client.get_detail(LAW_T, rid)
    except law_client.LawApiError as e:
        steps.append({"name": "조문 조회 실패", "detail": str(e)[:120]})
        return None

    def _no(x):
        return str(x or "").strip().lstrip("0")

    arts_all = [a for a in detail.get("articles", [])
                if a.get("조문여부") != "전문" and _no(a.get("조문번호"))]
    arts = [a for a in arts_all
            if _no(a.get("조문번호")) == it.jo and _no(a.get("조문가지번호")) == _no(it.gaji)]
    label = f"제{it.jo}조" + (f"의{it.gaji}" if it.gaji else "")
    f = {
        "id": rid, "target": LAW_T,
        "level": law_client.pick(row, "법령구분명", default="법령"),
        "mst": law_client.pick(row, "법령일련번호"),
        "name": name,
        "kind": law_client.pick(row, "법령구분명", default="법령"),
        "enforced": law_client.pick(row, "시행일자"),
        "promulgation_no": law_client.pick(row, "공포번호"),
        "ministry": law_client.pick(row, "소관부처명"),
        "meta": detail.get("meta", {}),
        "articles": arts,
    }
    steps.append({"name": "조문 직접 조회", "detail": f"「{name}」 {label}"
                  + ("" if arts else " — 해당 조문 없음")})
    if not arts:
        nums = sorted({int(_no(a.get("조문번호"))) for a in arts_all if _no(a.get("조문번호")).isdigit()})
        rng = f"제{nums[0]}조 ~ 제{nums[-1]}조" if nums else "확인 불가"
        return {"steps": steps, "laws": [f], "citations": [], "warnings": [],
                "answer": (f"【결론】\n「{name}」에서 {label}를 찾지 못했습니다. "
                           f"이 법령의 조문 범위는 {rng} 입니다. 조문 번호를 다시 확인해 주세요."),
                "intent": it.as_dict(), "debug": {"context_keys": [], "dropped_keys": []}}

    a = arts[0]
    title = a.get("조문제목", "")
    body = a.get("조문내용", "")
    context = (f"=== 여기부터는 「{name}」 조문입니다 (시행 {f.get('enforced') or '?'}) ===\n"
               f"[{name}] {label}" + (f"({title})" if title else "") + f"\n{body}")
    q = (req.question + "\n(질문 유형: 조문 조회 — 이 조문의 내용을 항·호 순서대로 쉽게 풀어 설명한다. "
         "다른 조문·하위법령·별표에 넘기는 부분은 넘긴다는 사실만 적고 내용을 지어내지 않는다)")
    try:
        text = ai_client.answer(q, context)
    except ai_client.AiError as e:
        return _err(str(e))
    # ★ v1.32 — 가지조문 번호가 빠지는 오타 보정: "제39조의제1항" → "제39조의3제1항".
    #   (실제 사례: 시행령 제39조의3 조회 답변의 【근거】 네 줄이 모두 "제39조의제N항")
    if it.gaji:
        text = re.sub(rf"제{it.jo}조의(?=\s*제\d|\s*\||\s*$)", f"제{it.jo}조의{it.gaji}", text, flags=re.M)
    cites = verify_citations(text, [f])
    steps.append({"name": "인용 검증", "detail": f"{sum(1 for c in cites if c['ok'])}/{len(cites)}건 확인"})
    hitk = sorted({(c["law"], c["jo"], c["gaji"]) for c in cites if c["ok"]})
    return {
        "steps": steps, "answer": text, "laws": [f], "citations": cites,
        "cited_keys": [f"{n}|{j}|{g}" for n, j, g in hitk],
        "references": [], "warnings": [],
        "intent": it.as_dict(),
        "debug": {"flat_n": len(arts_all), "picked_n": 1,
                  "context_keys": [_ctx_key(f, a)], "dropped_keys": []},
    }


class _LoggedSteps(list):
    """steps.append 할 때마다 서버 로그에도 한 줄 남깁니다 (applog.step)."""

    def append(self, item):
        super().append(item)
        try:
            applog.step(item.get("name", ""), item.get("detail"))
        except Exception:                                  # noqa: BLE001
            pass


def _ask_logged(req: AskRequest, progress: list):
    """
    _ask_sync 를 질문 블록 로그로 감쌉니다.

      ▶ START  새 질문(round 0)
        │ 단계들 …
        │ ? 되묻기 N차 — 사용자 응답 대기     ← 블록을 닫지 않음
        │ ↩ 되묻기 N차 응답 …                ← 같은 번호로 이어서
      ■ END    최종 답변 또는 오류
    """
    sess = applog.begin((req.question.strip(), req.target), req.question.strip(),
                        req.round, req.answered or "", req.note or "")
    try:
        result = _ask_sync(req, progress)
    except Exception as e:
        applog.end(sess, error=f"{type(e).__name__}: {e}")
        raise
    try:
        if isinstance(result, JSONResponse):
            body = json.loads(bytes(result.body).decode("utf-8"))
            applog.end(sess, error=body.get("error") or "오류 응답")
        elif isinstance(result, dict) and result.get("clarify"):
            applog.pause(sess, result["clarify"], result.get("round"))
        elif isinstance(result, dict):
            cites = result.get("citations") or []
            ans = str(result.get("answer") or "")
            if cites:
                ok = sum(1 for c in cites if c.get("ok"))
                summary = f"답변 {len(ans):,}자 · 인용 검증 {ok}/{len(cites)}"
            elif result.get("laws") and len(ans) > 100:
                summary = f"답변 {len(ans):,}자 · 인용 없음"
            else:
                summary = f"결과: {ans[:60]}"
            if result.get("warnings"):
                summary += f" · 경고 {len(result['warnings'])}건"
            applog.end(sess, summary=summary)
        else:
            applog.clear()
    except Exception:                                      # noqa: BLE001
        applog.clear()
    return result


def _ask_sync(req: AskRequest, progress: list):
    """
    질문 → (갈래 판단) → 용어변환 → 체계도 기반 계층 검색 → 조문 → 답변 → 검증
    """
    steps = progress          # 화면으로 실시간 전달되는 목록

    # 사용자가 직접 적은 내용을 조건에 합칩니다.
    answered = " / ".join(x for x in (req.answered, req.note.strip()) if x)

    # ★ v1.31 — 질문 의도(Intent). 규칙 기반이라 LLM 호출이 없습니다(intent.py).
    #   경로가 갈립니다: 조문 조회는 그 조문만, 정의·일반 기준은 되묻기 없이 답변.
    it = intent_mod.classify(req.question)
    if req.round == 0:
        steps.append({"name": "질문 유형",
                      "detail": f"{it.label} — " + ", ".join(it.signals)[:100]})
    if it.kind == intent_mod.ARTICLE_LOOKUP and req.target in ("auto", "law"):
        direct = _ask_article(req, steps, it)
        if direct is not None:
            return direct
        steps.append({"name": "조문 직접 조회 실패", "detail": "일반 검색으로 진행합니다"})
        it = intent_mod.Intent(intent_mod.OTHER, intent_mod.LABEL[intent_mod.OTHER], True,
                               signals=["조문 직접 조회 실패"])

    # 되묻기 라운드가 넘어갈 때마다 아래 1~3단계를 다시 실행하고 있었습니다.
    # 검색 결과는 되묻기 답변과 무관하게 같으므로(질문·대상이 같으면 같은 법령),
    # 조건은 키에서 뺍니다. 조건은 답변 생성에만 쓰입니다.
    user_laws = _user_laws(req)
    cache_key = (req.question.strip(), req.target, tuple(user_laws), _laws_mode(req) if user_laws else "")
    # ★ 새 질문(round=0)이면 캐시를 쓰지 않고 반드시 새로 검색합니다.
    #   되묻기 라운드 중(round>0)에만 재사용합니다.
    #   이것이 없으면 이전 질문의 법령이 그대로 남아 엉뚱한 답이 나옵니다.
    cached = _cache_get(cache_key) if req.round > 0 else None
    if req.round == 0:
        _SEARCH_CACHE.pop(cache_key, None)
        _SEARCH_CACHE.pop(("sel",) + cache_key, None)       # v1.31 조문 선별 캐시
    if cached:
        found, flat, catalog, refs, ai_hits = cached
        steps.append({"name": "이전 검색 재사용",
                      "detail": f"{len(found)}개 법령 / 조문 {len(flat)}개"})
        return _ask_after_search(req, steps, answered, found, flat, catalog, refs, it, ai_hits)

    # --- 1단계: 법령 용어로 변환 ----------------------------------
    try:
        terms = ai_client.extract_terms(req.question + (f"\n(조건: {answered})" if answered else ""))
    except ai_client.AiError as e:
        return _err(str(e))
    steps.append({"name": "검색어 변환", "detail": terms})

    names = terms.get("법령명", [])
    words = terms.get("용어", [])

    # ★ v1.31 — 법제처 지능형 검색: 질문 **원문**을 넣어 관련 조문을 관련도 순으로 받습니다.
    #   로컬 LLM 의 법령명 추측(가끔 없는 법을 지어냄)과 가나다순 본문검색을 보완합니다.
    #   실패하거나 비어 있으면 예전 방식 그대로 갑니다.
    ai_hits, ai_laws, ai_score = [], [], {}
    if req.target in ("auto", "law"):
        ai_rows = []
        # ★ v1.32 — 같은 질문이 어떤 때는 결과 0건으로 옵니다(실측: 21:18 20건 → 22:04 0건).
        #   비어 있으면 1초 뒤 한 번 더 부릅니다.
        # ★ v1.33 — 그래도 비면 3초 뒤 한 번 더, 마지막으로 추출한 용어로 한 번 더 찾습니다.
        tries = [(req.question, 0.0), (req.question, 1.0), (req.question, 3.0)]
        if words:
            tries.append((" ".join(words[:3]), 0.0))
        for attempt, (aq, wait) in enumerate(tries, 1):
            if wait:
                time.sleep(wait)
            try:
                ai_rows = law_client.ai_search(aq, display=20)
            except Exception as e:                          # noqa: BLE001
                ai_rows = []
                applog.warn(f"지능형 검색 실패({attempt}차): {e}")
            if ai_rows:
                if aq != req.question:
                    steps.append({"name": "지능형 검색(용어)", "detail": f"질문 원문으로 결과가 없어 '{aq}' 로 찾음"})
                break
        # ★ v1.32 — 법령 순위 = 그 법령(시행령·시행규칙 포함) 조문들의 **순위 역수 합** Σ 1/(1+순위).
        #   "처음 나온 순서" 는 1위 조문 하나에 좌우되고, "조문 개수" 는 하위권에 잔뜩 걸린 법이 이깁니다.
        #   실측(_eval/probe_ai_1.xml, "주유소 지하 저장시설 누출검사 주기"):
        #     토양환경보전법 1·6위 → 1.17 / 액화석유가스법 4·7·14·18·19위 → 0.57 (개수로는 5 대 2 로 역전됐던 것)
        score, first = {}, {}
        for i, r in enumerate(ai_rows):
            nm = (r.get("법령명") or "").strip()
            base = re.sub(r"\s*(시행령|시행규칙)$", "", nm).strip()
            if base:
                score[base] = score.get(base, 0.0) + 1.0 / (1 + i)
                first.setdefault(base, i)
            if nm and r.get("조문번호"):
                ai_hits.append((nm, r.get("조문번호", ""), r.get("조문가지번호", "")))
        ai_laws = sorted(score, key=lambda b: (-score[b], first[b]))
        ai_score = score
        if ai_laws:
            steps.append({"name": "지능형 검색",
                          "detail": "관련 법령(관련도 점수 순): "
                                    + ", ".join(f"{b}({score[b]:.2f})" for b in ai_laws[:5])
                                    + f" · 조문 {len(ai_hits)}개"})
        elif ai_rows == []:
            steps.append({"name": "지능형 검색", "detail": "결과 없음"})

    # ★ v1.33 — 법령이 애매하면 첫 라운드에 "어느 법" 인지 묻습니다(조문 수집 전에 — 헛수집을 줄임).
    if (not user_laws and req.round == 0 and not req.skip_clarify
            and req.target in ("auto", "law")):
        tot = sum(ai_score.values()) or 1.0
        share = ai_score.get(ai_laws[0], 0.0) / tot if ai_laws else 0.0
        guess_keys = {_law_key(re.sub(r"\s*(시행령|시행규칙)$", "", n)) for n in names}
        ambiguous = bool(ai_laws) and share < LAW_ASK_SHARE and _law_key(ai_laws[0]) not in guess_keys
        if not ai_laws:
            # 지능형 검색이 비었는데 AI 가 추측한 법령도 실제로 없으면(지어낸 이름) 근거가 하나도 없습니다.
            #   실측: "유류판매업법"(없는 법) → 본문검색 복구가 산업집적법을 잡아 엉뚱한 답.
            real = []
            for n in names:
                b = re.sub(r"\s*(시행령|시행규칙)$", "", n).strip()
                rows = _stmd_rows(b)
                if any(_law_key(law_client.row_name("lsStmd", r)) == _law_key(b) for r in rows):
                    real.append(b)
            ambiguous = not real
        if ambiguous:
            opts = []
            for b in ai_laws:
                if len(opts) >= 3:
                    break
                if ai_score.get(b, 0) < 0.15 or re.search(r"(직제|고시|훈령|예규|규정|지침)$", b):
                    continue
                opts.append(b)
            for n in names:                       # AI 추측 중 **실제로 있는** 법령
                if len(opts) >= 4:
                    break
                b = re.sub(r"\s*(시행령|시행규칙)$", "", n).strip()
                if any(_law_key(b) == _law_key(o) for o in opts):
                    continue
                rows = _stmd_rows(b)
                row = _pick_stmd(rows, b) if rows else None
                if row is not None and _law_key(law_client.row_name("lsStmd", row)) == _law_key(b):
                    opts.append(b)
            if opts or not ai_laws:
                steps.append({"name": "법령 확인 필요",
                              "detail": (f"지능형 검색 1위 '{ai_laws[0]}' 비중 {share:.0%}, "
                                         f"AI 추측 '{', '.join(names) or '없음'}' 와 달라 어느 법인지 묻습니다"
                                         if ai_laws else
                                         f"지능형 검색 결과가 없고 AI 추측 '{', '.join(names) or '없음'}' 도 "
                                         f"법제처에 없는 이름이라 어느 법인지 묻습니다")})
                return {
                    "clarify": [{"question": LAW_Q, "options": opts + [LAW_Q_DIRECT, LAW_Q_UNKNOWN]}],
                    "round": req.round + 1,
                    "max_rounds": CLARIFY_ROUNDS + 1,
                    "answered": answered,
                    "steps": steps,
                    "intent": it.as_dict(),
                }

    if not names and not words and not ai_laws:
        return {"steps": steps, "answer": "검색어를 만들지 못했습니다. 질문을 더 구체적으로 써보세요.", "laws": []}

    found, seen = [], set()

    def add(target, query, limit=2, level="", scope=1):
        if target not in law_client.TARGETS:
            steps.append({"name": "경고", "detail": f"알 수 없는 검색 대상: {target}"})
            return
        try:
            rows = law_client.search(target, query, scope=scope)
        except law_client.LawApiError as e:
            steps.append({"name": "검색 실패", "detail": f"{target}: {e}"})
            return
        picked_n = 0
        for r in rows:
            if picked_n >= limit:
                break
            # 자치법규는 다른 지자체 조례가 섞여 옵니다.
            # 지자체기관명에 우리 지자체가 없으면 버립니다.
            if target == "ordin" and LOCAL_GOV:
                org = law_client.pick(r, "지자체기관명", "지자체명")
                if LOCAL_GOV not in org:
                    continue

            rid = law_client.row_id(target, r)
            if not rid or rid in seen:
                continue
            seen.add(rid)
            picked_n += 1
            found.append({
                "id": rid, "target": target, "level": level,
                "mst": law_client.pick(r, "법령일련번호", "행정규칙일련번호", "자치법규일련번호"),
                "name": law_client.row_name(target, r),
                "kind": law_client.pick(r, "법령구분명", "행정규칙종류", "자치법규종류", default=TARGET_LABEL.get(target, "")),
                "enforced": law_client.pick(r, "시행일자", "회신일자"),
                "promulgation_no": law_client.pick(r, "공포번호", "발령번호", "안건번호"),
                "ministry": law_client.pick(r, "소관부처명", "지자체기관명", "회신기관명"),
            })

    # --- 2단계: 체계도로 계층 전체 확보 ---------------------------
    LAW_T = law_client.LAW_TARGET          # 기본 eflaw(시행일 기준)

    if req.target in ("auto", "law"):
        # ★ AI 가 추측한 법령명이 틀리면 체계도가 통째로 비고, 그 뒤 모든 단계가
        #   엉뚱한 법령 위에서 돌아갑니다. 이 도구의 가장 큰 약점이었습니다.
        #   그래서 추측한 이름으로 체계도가 안 잡히면, 용어로 **본문 검색**을 해서
        #   그 말이 실제로 들어 있는 법령의 **진짜 이름**을 법제처에서 받아옵니다.
        #   법령명을 맞힐 필요가 없어집니다.
        # ★ v1.33 — 사용자가 법령을 지정했으면(화면 입력 또는 "어느 법" 되묻기 답) 그 법령만 수집합니다.
        user_resolved, user_bad = [], []
        for u in user_laws:
            b = re.sub(r"\s*(시행령|시행규칙)$", "", u).strip()
            hit_name = ""
            for qn in (b, ):
                rows = _stmd_rows(qn)
                exact = [r for r in rows if _law_key(law_client.row_name("lsStmd", r)) == _law_key(qn)]
                if exact:
                    hit_name = law_client.row_name("lsStmd", exact[0])
            if not hit_name:
                try:
                    full = law_client.resolve_abbrev(b)
                except Exception:                         # noqa: BLE001
                    full = b
                if full and full != b:
                    rows = _stmd_rows(full)
                    exact = [r for r in rows if _law_key(law_client.row_name("lsStmd", r)) == _law_key(full)]
                    if exact:
                        hit_name = law_client.row_name("lsStmd", exact[0])
            if hit_name:
                if not any(_law_key(hit_name) == _law_key(x) for x in user_resolved):
                    user_resolved.append(hit_name)
            else:
                user_bad.append(u)
        only_mode = _laws_mode(req) == "only"
        if user_laws:
            steps.append({"name": "사용자 지정 법령" + (" (이 법령에서만)" if only_mode else " (우선 참고)"),
                          "detail": (", ".join(user_resolved) or "확인된 법령 없음")
                                    + (f" · 찾지 못함: {', '.join(user_bad)}" if user_bad else "")
                                    + ("" if user_resolved else " — 자동 검색으로 진행")})

        cand = list(names[:2])
        if cand:
            hit = False
            for nm in cand:
                base = nm.replace(" 시행령", "").replace(" 시행규칙", "").strip()
                if _stmd_rows(base):
                    hit = True
                    break

            # ★ 약칭(법제처 공식 약칭 DB)이면 정식명으로 바꿔 한 번 더 시도합니다.
            #   본문 검색(아래)보다 가볍고 정확해서 먼저 시도합니다.
            #   ("영"/"규칙" 같은 축약은 이거로 안 잡힙니다 — 그건 _guess_law 의
            #    별도 로직입니다. 이건 "개인정보법" 처럼 법제처가 공식 등록한
            #    약칭용입니다.)
            if not hit:
                resolved = []
                for nm in cand:
                    base = nm.replace(" 시행령", "").replace(" 시행규칙", "").strip()
                    try:
                        full = law_client.resolve_abbrev(base)
                    except Exception:
                        full = base
                    if full != base and full not in resolved:
                        resolved.append(full)
                for full in resolved:
                    try:
                        if _stmd_rows(full):
                            hit = True
                            steps.append({
                                "name": "법령명 복구(약칭)",
                                "detail": f"'{', '.join(cand)}' 을(를) 공식 약칭으로 보고 "
                                          f"'{full}' 로 재시도",
                            })
                            cand = [full] + cand
                            break
                    except law_client.LawApiError:
                        pass

            # ★ 2026-08-20 — 여기까지는 "AI 가 추측한 이름이 법제처에 있는지"만 봤습니다.
            #   그런데 **존재는 하지만 엉뚱한 법**을 추측하면(예: 음식점 폐식용유 질문에
            #   "방사성폐기물 관리법") hit=True 로 확정되고 끝나버립니다. 로컬 모델은
            #   이런 지식 기반 실수를 대형 클라우드 모델보다 훨씬 자주 냅니다.
            #   그래서 hit 여부와 무관하게, 추출된 용어로 본문검색을 걸어서
            #   "실제 조문에 이 말이 들어 있는 법" 과 AI 추측을 항상 교차검증합니다.
            #   AI 지식이 아니라 법제처 원문이 최종 판단 기준이 됩니다.
            # ★ 2026-09-29 — 본문검색 결과는 관련도 순이 아니라 **법령명 가나다순**이고
            #   한 번에 최대 100개입니다. 그래서
            #   · 결과 100개 전체를 보고, 여러 용어에 반복해서 걸린 법을 앞세웁니다.
            #   · 결과가 100개로 꽉 찬 용어("시정명령", "신고" 같은 흔한 말)는 근거로
            #     쓰지 않습니다. 가나다순으로 잘려 뒤쪽 법(예: ㅌ·ㅍ·ㅎ)이 안 보이므로,
            #     "추측한 법이 결과에 없다" 는 판단이 틀립니다.
            #     (실사고: "소음·진동관리법" 추측이 뒤집혀 "가덕도신공항 건설을 위한
            #      특별법" 으로 답할 뻔함)
            hits_all: dict[str, int] = {}
            hits_info: dict[str, int] = {}
            info_words, weak_words = [], []
            if words:
                for w in words[:3]:
                    try:
                        rows = law_client.search(LAW_T, w, display=100, scope=2)   # 본문 검색
                    except law_client.LawApiError:
                        continue
                    if not rows:
                        continue
                    informative = len(rows) < 100
                    (info_words if informative else weak_words).append(w)
                    seen_w = set()
                    for r in rows:
                        nm = law_client.row_name(LAW_T, r)
                        # 시행령·시행규칙은 체계도가 알아서 따라옵니다. 본법만 모읍니다.
                        nm = re.sub(r"\s*(시행령|시행규칙)$", "", nm).strip()
                        if nm and nm not in seen_w:
                            seen_w.add(nm)
                            hits_all[nm] = hits_all.get(nm, 0) + 1
                            if informative:
                                hits_info[nm] = hits_info.get(nm, 0) + 1

            def _ranked(h: dict) -> list:
                # 많이 걸린 순. 같으면 검색 결과 순서 유지(sorted 는 안정 정렬).
                return sorted(h, key=lambda n: -h[n])

            def _same(a: str, b: str) -> bool:
                # 부분 일치로 비교하면 "폐기물관리법" 이 "방사성폐기물관리법" 과
                # 같다고 나옵니다. 공백·가운뎃점·하위법령 꼬리만 떼고 정확히 비교합니다.
                return _law_key(a) == _law_key(b)

            if not hit:
                recovered = _ranked(hits_info or hits_all)
                fresh = [nm for nm in recovered if not any(_same(nm, c) for c in cand)]
                if fresh:
                    steps.append({
                        "name": "법령명 복구(본문 검색)",
                        "detail": f"'{', '.join(cand)}' 로 체계도를 찾지 못해 "
                                  f"'{', '.join(words[:3])}' 본문 검색 → "
                                  + ", ".join(fresh[:3]),
                    })
                    cand = fresh[:2] + cand
            elif hits_info and not any(_same(nm, c) for nm in hits_info for c in cand):
                # AI 가 추측한 법이 존재는 하지만, 드문 용어가 들어 있는 법 어디에도 없습니다.
                # 다만 근거가 한 법에 모여 있을 때만 뒤집습니다(1등이 3개 이하).
                # 여러 법이 똑같이 걸리면 가나다순 첫 법을 고르는 셈이라 믿을 수 없습니다.
                ranked = _ranked(hits_info)
                best = hits_info[ranked[0]]
                tops = [n for n in ranked if hits_info[n] == best]
                if len(tops) <= 3:
                    steps.append({
                        "name": "법령명 교차검증 실패 → 본문 검색 우선",
                        "detail": f"AI 추측 '{', '.join(cand)}' 이(가) '{', '.join(info_words)}' "
                                  f"본문검색 결과에 없어 '{ranked[0]}' 을(를) 먼저 봅니다.",
                    })
                    cand = ranked[:1] + cand
                else:
                    steps.append({
                        "name": "법령명 교차검증 보류",
                        "detail": f"'{', '.join(info_words)}' 가 든 법이 {len(tops)}개로 고르게 "
                                  f"퍼져 근거가 약함 — AI 추측 '{', '.join(cand)}' 유지",
                    })

        # ★ v1.31 — 지능형 검색 1순위 법령을 후보 맨 앞에 둡니다. 로컬 LLM 의 추측은
        #   (체계도에 실제로 있으면) 2순위로 남겨 둡니다 — 둘 다 수집하고 조문 선별이 고릅니다.
        if ai_laws and not (user_resolved and only_mode):
            top = ai_laws[0]
            if not any(_law_key(top) == _law_key(c) for c in cand):
                steps.append({"name": "법령 후보 보정(지능형 검색)",
                              "detail": f"'{top}' 을(를) 먼저 봅니다"
                                        + (f" (AI 추측: {', '.join(cand)})" if cand else "")})
            rest = [c for c in cand if _law_key(c) != _law_key(top)]
            # ★ v1.32 — 나머지 후보 중 지능형 검색에도 나온 것(교차 확인된 것)을 앞으로.
            #   본문검색 복구가 가나다순으로 올린 엉뚱한 법(건설기계 안전기준 등)이 2순위를 차지하지 않게.
            ai_keys = {_law_key(x) for x in ai_laws[:5]}
            corr = [c for c in rest if _law_key(c) in ai_keys]
            others = [c for c in rest if _law_key(c) not in ai_keys]
            # ★ v1.32 — 법령 후보를 3개까지 수집합니다(2026-10-08 사용자 결정 "법령 후보 3개로 확대").
            #   지능형 검색 2위 법령도 후보에 넣습니다. 단 점수가 낮거나(상위 5위 안 조문이 없음 ≈ 0.2 미만)
            #   직제·고시처럼 조문 답변 근거가 될 수 없는 것은 넣지 않습니다.
            ai2 = [b for b in ai_laws[1:3]
                   if ai_score.get(b, 0) >= 0.2 and not re.search(r"(직제|고시|훈령|예규|규정)$", b)
                   and not any(_law_key(b) == _law_key(c) for c in [top] + corr)]
            cand = [top] + corr + ai2[:1] + [c for c in others
                                              if not any(_law_key(c) == _law_key(x) for x in ai2[:1])]

        # v1.3 의 조문 선별이 붙어 토큰 부담이 크게 줄었으므로 후보를 2개로 되돌립니다.
        # 1개만 쓰면 "누출검사" 처럼 여러 법에 쓰이는 용어에서 엉뚱한 법 하나만
        # 잡고 끝나 답이 통째로 틀립니다. (토양환경보전법 → 위험물안전관리법)
        # ★ v1.32 — 후보 하나가 체계도 조회에 실패하면 **조용히 건너뛰고** 나머지 하나로만
        #   답하던 문제. 실패를 처리 과정에 남기고, 성공 2개가 될 때까지 다음 후보로 넘어갑니다.
        if user_resolved and only_mode:
            cand = list(user_resolved)
            cand_limit = len(cand)
        elif user_resolved:
            # 우선 참고: 지정 법령을 맨 앞에 두고, 자동 후보로 한 묶음 이상 더 수집합니다.
            cand = list(user_resolved) + [c for c in cand
                                          if not any(_law_key(c) == _law_key(u) for u in user_resolved)]
            cand_limit = max(LAW_CANDIDATES, len(user_resolved) + 1)
        else:
            cand_limit = LAW_CANDIDATES
        stmd_ok, stmd_fail = 0, []
        for nm in cand:
            if stmd_ok >= cand_limit:
                break
            base = nm.replace(" 시행령", "").replace(" 시행규칙", "").strip()
            rows = _stmd_rows(base)
            row = _pick_stmd(rows, base) if rows else None
            if row is None:
                stmd_fail.append(f"{base}(체계도 검색 결과 없음)")
                continue
            mst = law_client.pick(row, "법령일련번호")
            if not mst:
                stmd_fail.append(f"{base}(법령일련번호 없음)")
                continue
            try:
                tree = law_client.get_hierarchy(mst)
            except law_client.LawApiError as e:
                stmd_fail.append(f"{base}(체계도 조회 실패: {str(e)[:60]})")
                continue
            stmd_ok += 1
            for L in tree["laws"]:                       # 법률·시행령·시행규칙
                if L["id"] and L["id"] not in seen:
                    seen.add(L["id"])
                    # mst(법령일련번호) 는 법제처 3단비교 URL 의 lsiSeq 로 씁니다.
                    found.append({**L, "target": LAW_T, "kind": L["level"], "ministry": ""})
            # 위임행정규칙은 체계도에 딸려오지만 질문과 무관한 것이 섞입니다.
            # (예: 토양환경보전법 체계도에 "금강수계 수변구역 변경" 이 포함됨)
            # 검색어와 겹치는 것을 우선하고, 겹치는 게 없으면 앞에서부터 씁니다.
            # 검색어를 2글자 조각으로 쪼개 부분 일치도 잡습니다.
            #   "특정토양오염관리대상시설" → 토양·오염·관리·시설 …
            frags = set()
            for w in (names + words):
                w = re.sub(r"(법|시행령|시행규칙)$", "", w)
                for i in range(len(w) - 1):
                    frags.add(w[i:i + 2])

            def _score(ar):
                nm = ar.get("name", "")
                return sum(1 for fr in frags if fr in nm)

            ranked = sorted(tree["admruls"], key=lambda x: -_score(x))
            # 겹치는 조각이 하나도 없으면(0점) 무관한 것으로 보고 버립니다.
            # 나머지는 점수 순으로 최대 4개.
            picked_admruls = [a for a in ranked if _score(a) > 0][:4]

            for A in picked_admruls:                     # 위임행정규칙
                if A["id"] not in seen:
                    seen.add(A["id"])
                    found.append({
                        "id": A["id"], "target": "admrul",
                        "level": f"{A['level']} 위임", "name": A["name"],
                        "kind": A["kind"], "enforced": A["enforced"],
                        "promulgation_no": A["promulgation_no"], "ministry": "",
                    })
        if stmd_fail:
            applog.warn("법령 후보 체계도 실패: " + ", ".join(stmd_fail))
            steps.append({"name": "법령 후보 일부 실패",
                          "detail": ", ".join(stmd_fail[:3])
                                    + (" — 다음 후보로 대신했습니다" if stmd_ok else "")})

    # 체계도로 못 찾았으면 일반 검색으로 보완
    if not found:
        if req.target in ("auto", "law"):
            for q in (names + words)[:3]:
                add(LAW_T, q, 2, "법령")
            # 법령명 검색도 실패했으면 마지막으로 본문 검색을 겁니다.
            # 법령명을 몰라도 그 말이 들어간 법령을 찾아냅니다.
            if not found:
                for q in (words + names)[:2]:
                    add(LAW_T, q, 2, "법령", scope=2)
                if found:
                    steps.append({"name": "본문 검색으로 확보",
                                  "detail": ", ".join(f["name"] for f in found[:4])})
        if req.target in ("auto", "admrul"):
            for q in (names + words)[:2]:
                add("admrul", q, 2, "행정규칙")
    # ★ v1.31 — 자동(auto) 검색에서 조례를 뺍니다. 지금 범위는 국가법령이고,
    #   LOCAL_GOV(특정 지자체)에 묶인 결과가 범용 답변을 흐렸습니다.
    #   조례는 화면에서 대상으로 "자치법규" 를 직접 고를 때만 찾습니다.
    if req.target == "ordin":
        # 조례는 "성남시 토양환경보전법" 같은 이름일 수 없습니다.
        # 법령명이 아니라 용어(누출검사, 토양오염)로 찾아야 합니다.
        gov = LOCAL_GOV or ""
        for q in (words or names)[:2]:
            q = q.strip()
            add("ordin", q if (gov and gov in q) else f"{gov} {q}".strip(), 2, "자치법규")
    if req.target == "expc":
        for q in (names + words)[:3]:
            add("expc", q, 3, "해석례")

    steps.append({"name": "법령 검색", "detail": [f"{f.get('level') or f.get('kind')}: {f['name']}" for f in found]})
    if not found:
        return {"steps": steps, "answer": "해당하는 법령을 찾지 못했습니다.", "laws": []}

    # --- 3단계: 조문 수집 ---------------------------------------
    refs = {}
    for f in found[:7]:
        try:
            detail = law_client.get_detail(f["target"], f["id"])
        except law_client.LawApiError:
            continue
        f["meta"] = detail["meta"]
        f["articles"] = detail["articles"]

        # ★ 법률 조문에는 위임법령(lsDelegated) 매핑을 붙여둡니다.
        #   시행령·시행규칙 자체에는 안 붙입니다 — 위임은 "법률 조문 → 하위법령
        #   조문" 방향으로만 의미가 있습니다. 실패해도 조문 조회는 계속됩니다
        #   (delegation_map 자체가 예외를 삼킵니다).
        if f.get("level") == "법률" and f.get("id"):
            f["delegated"] = law_client.delegation_map(f["id"])

        if f["target"] == "ordin":
            real = detail["meta"].get("자치법규명", "")
            if real and f["name"] and real.strip() != f["name"].strip():
                f["articles"] = []
                f["warning"] = f"조회 결과가 '{real}' 로 나와 제외했습니다."
                continue
            org = detail["meta"].get("지자체기관명", "")
            if LOCAL_GOV and org and LOCAL_GOV not in org:
                f["articles"] = []
                f["warning"] = f"{org} 조례여서 제외했습니다 (설정: {LOCAL_GOV})."
                continue

        for r in law_client.extract_references(detail["articles"], f["name"]):
            refs[r] = refs.get(r, 0) + 1

    # 조문을 하나의 목록으로 펼칩니다. (법령, 조문) 쌍에 번호를 매깁니다.
    flat = []
    for f in found[:7]:
        for a in f.get("articles", []):
            if a.get("조문여부") == "전문":
                continue
            if not (a.get("조문내용") or "").strip():
                continue
            flat.append((f, a))

    if not flat:
        return {"steps": steps, "answer": "조문 본문을 가져오지 못했습니다.", "laws": found}

    def label_of(a):
        no, gaji = a.get("조문번호", ""), a.get("조문가지번호", "")
        title = a.get("조문제목", "")
        if not no:
            return title or "(제목 없음)"
        return f"제{no}조" + (f"의{gaji}" if gaji else "") + (f"({title})" if title else "")

    catalog = "\n".join(
        f"{i+1}. [{f['name']}] {label_of(a)}" for i, (f, a) in enumerate(flat)
    )

    _cache_put(cache_key, (found, flat, catalog, refs, ai_hits))
    return _ask_after_search(req, steps, answered, found, flat, catalog, refs, it, ai_hits)


def _ask_after_search(req, steps, answered, found, flat, catalog, refs, it=None, ai_hits=None):
    """검색이 끝난 뒤의 단계. 되묻기 라운드마다 여기부터 다시 실행됩니다."""
    # ★ 2026-08-19 — 사용자가 "아니오" 라고 답한 주제의 법령을 빼는 처리가
    #   예전에는 **검색 경로 안에만** 있었습니다. 그런데 그 경로는 round=0
    #   (= answered 가 비어 있는 새 질문)에서만 지나가고, 답이 실제로 들어오는
    #   round>=1 은 항상 캐시로 빠져나가 이 필터를 건너뛰었습니다.
    #   즉 "방사성폐기물에 해당하나요? → 아니오" 라고 답해도 방사성폐기물
    #   관련 법령이 그대로 남아 답변 컨텍스트에 들어갔습니다. 기능이 죽어 있었죠.
    #   → 되묻기 답을 실제로 손에 쥔 이 지점으로 옮깁니다.
    #     캐시가 오염되지 않도록 **사본**에만 적용합니다.
    excluded = _excluded_terms(answered)
    if excluded:
        dropped = [f for f in found
                   if any(x in f.get("name", "") for x in excluded)]
        if dropped and len(dropped) < len(found):   # 전부 걸리면 거르지 않습니다
            drop_ids = {id(d) for d in dropped}
            found = [f for f in found if id(f) not in drop_ids]
            flat = [(f, a) for f, a in flat if id(f) not in drop_ids]
            catalog = "\n".join(
                f"{i+1}. [{f['name']}] "
                + (lambda no, gaji, t: (f"제{no}조" + (f"의{gaji}" if gaji else "")
                                        + (f"({t})" if t else "")) if no else (t or "(제목 없음)"))(
                    a.get("조문번호", ""), a.get("조문가지번호", ""), a.get("조문제목", ""))
                for i, (f, a) in enumerate(flat))
            steps.append({
                "name": "제외 조건 적용",
                "detail": f"'{', '.join(excluded)}' 아님 → "
                          + ", ".join(d["name"] for d in dropped[:4]) + " 제외",
            })

    def label_of(a):
        no, gaji = a.get("조문번호", ""), a.get("조문가지번호", "")
        title = a.get("조문제목", "")
        if not no:
            return title or "(제목 없음)"
        return f"제{no}조" + (f"의{gaji}" if gaji else "") + (f"({title})" if title else "")

    if it is None:
        it = intent_mod.classify(req.question)
    MAX_ROUNDS = CLARIFY_ROUNDS

    # --- 3-1단계: 필요한 조문만 고르기 ----------------------------
    # 조문 본문을 통째로 넣으면 요청당 3만 토큰. 제목만 보여주고 고르면 2천 토큰.
    # ★ v1.31 — 선별을 되묻기 **앞**으로 옮겼습니다. 되묻기가 선별된 조문 본문을
    #   보고 판단하기 때문입니다. 선별은 질문에만 달려 있으므로(되묻기 답과 무관)
    #   첫 라운드 결과를 조문 키로 저장해 두고 다음 라운드에서 재사용합니다
    #   (라운드마다 LLM 을 다시 부르지 않음). 번호가 아니라 키로 저장하는 이유:
    #   "아니오" 제외 조건으로 목록이 줄면 번호가 밀립니다.
    def _akey(f, a):
        return (f["name"], str(a.get("조문번호", "")), str(a.get("조문가지번호", "")),
                a.get("조문제목", ""))

    _ul = _user_laws(req)
    sel_key = ("sel", req.question.strip(), req.target, tuple(_ul), _laws_mode(req) if _ul else "")
    picked = None
    cached_sel = _cache_get(sel_key) if req.round > 0 else None
    if cached_sel:
        idx = {_akey(f, a): (f, a) for f, a in flat}
        picked = [idx[k] for k in cached_sel if k in idx] or None
        if picked:
            steps.append({"name": "조문 선별 재사용",
                          "detail": f"이전 라운드에서 고른 {len(picked)}개"})
    elif SELECT_ARTICLES and len(flat) > 12:
        try:
            # ★ 2026-09-29 — 법령이 여럿이면 조문 목록만으로도 수천 토큰입니다.
            #   컨텍스트를 넘으면 목록 뒤쪽을 잘라서 보냅니다(번호는 그대로라 매핑 유지).
            cat_sel = catalog
            q_sel = req.question + (f"\n(사용자가 참고 법령으로 지정: {', '.join(_ul)} — 이 법령의 조문을 우선 고려)"
                                    if _ul else "")
            n_sel = ai_client.count_tokens(ai_client.select_prompt(q_sel, catalog))
            lim = ai_client.server_ctx() - ai_client.MAXTOK_SELECT - 64
            if n_sel > lim:
                lines = catalog.splitlines()
                keep_n = max(20, int(len(lines) * lim / n_sel * 0.95))
                cat_sel = "\n".join(lines[:keep_n])
                steps.append({"name": "조문 목록 축소",
                              "detail": f"선별 입력 {n_sel:,}토큰 > 한도 {lim:,} — "
                                        f"목록 {len(lines)}줄 중 앞 {keep_n}줄만 보냄"})
            nums = ai_client.select_articles(q_sel, cat_sel)
            picked = [flat[n - 1] for n in nums if 1 <= n <= len(flat)]
        except ai_client.AiError as e:
            applog.warn(f"조문 선별 실패 — 전체 조문을 씁니다: {e}")
            picked = None                      # 실패하면 전체를 씁니다
        if picked:
            steps.append({"name": "조문 선별",
                          "detail": f"{len(flat)}개 중 {len(picked)}개 선택"})
        # ★ v1.31 — 지능형 검색이 지목한 조문(관련도 상위 6개)은 선별 결과에 없어도 앞에 넣습니다.
        #   제목만 보는 선별이 놓치는 조문(예: 시행령 제8조 검사 주기)을 보완합니다.
        if ai_hits and picked is not None:
            def _n(x):
                return str(x or "").strip().lstrip("0")
            have = {_akey(f, a) for f, a in picked}
            extra, front, seen = [], [], set()
            for law, jo, gaji in ai_hits[:6]:
                for f, a in flat:
                    if (_law_key(f["name"]) == _law_key(law) and law.replace(" ", "") == f["name"].replace(" ", "")
                            and _n(a.get("조문번호")) == _n(jo) and _n(a.get("조문가지번호")) == _n(gaji)):
                        k = _akey(f, a)
                        if k in seen:
                            break
                        seen.add(k)
                        front.append((f, a))
                        if k not in have:
                            extra.append((f, a))
                        break
            # ★ v1.32 — 지능형 검색이 지목한 조문은 선별이 이미 골랐더라도 **맨 앞**(우선순위 최상)으로.
            #   토큰 예산을 넘으면 뒤에서부터 빼므로, 순서가 곧 우선순위입니다.
            #   (실제 사례: "폐기물처리업 종류" 에서 핵심인 법 제25조가 선별 목록 뒤쪽에 있어 빠졌음)
            if front:
                picked = front + [p for p in picked if _akey(*p) not in seen]
            if extra:
                steps.append({"name": "지능형 검색 조문 추가",
                              "detail": ", ".join(_ctx_key(f, a) for f, a in extra)})
            moved = [p for p in front if p not in extra]
            if moved:
                steps.append({"name": "지능형 검색 조문 우선",
                              "detail": ", ".join(_ctx_key(f, a) for f, a in moved)})
        if picked:
            _cache_put(sel_key, [_akey(f, a) for f, a in picked])

    # ★ 아래에서 별표를 덧붙이므로 반드시 복사본으로 씁니다.
    #   picked/flat 을 그대로 쓰면 append 가 원본 목록을 오염시킵니다.
    use = list(picked or flat)

    # ── v1.32 — 상위 조문 자동 포함 ─────────────────────────────────
    # 시행령·시행규칙 조문은 "법 제12조제1항에 따른 …", "영 제8조에 따라 …" 처럼 근거 조문을
    # 적습니다. 선별이 하위 조문만 고르고 그 근거(법률 조문)를 빠뜨리면 답변이 "신고 의무가 있다"
    # 는 근거를 못 댑니다. (실제 사례: 주유소 설치신고 — 시행규칙 제12조는 골랐지만 법 제12조 누락)
    # 선별 결과가 있을 때만, 최대 4개, 우선순위는 인용한 조문 바로 뒤.
    parent_rank: dict = {}
    if picked:
        def _nk(x):
            return str(x or "").strip().lstrip("0")
        flat_idx = {}
        for f, a in flat:
            if _nk(a.get("조문번호")):
                flat_idx.setdefault((f["name"], _nk(a.get("조문번호")), _nk(a.get("조문가지번호"))), (f, a))
        have_k = {(f["name"], _nk(a.get("조문번호")), _nk(a.get("조문가지번호"))) for f, a in use}
        parents = []
        for i_use, (f, a) in enumerate(list(use)):
            nm = f.get("name", "")
            if not nm.endswith(("시행령", "시행규칙")) or len(parents) >= 4:
                continue
            fam = re.sub(r"\s*(시행령|시행규칙)$", "", nm).strip()
            body = a.get("조문내용", "") or ""
            for m in re.finditer(r"(?<![가-힣「」])(법|영)\s*제\s*(\d+)\s*조(?:\s*의\s*(\d+))?", body):
                if body[max(0, m.start() - 3):m.start()].endswith("같은 "):
                    continue                     # "같은 법 제5조" — 앞에 나온 **다른** 법
                tgt = fam if m.group(1) == "법" else fam + " 시행령"
                k = (tgt, m.group(2), _nk(m.group(3)))
                if k in have_k or k not in flat_idx:
                    continue
                have_k.add(k)
                parents.append(flat_idx[k])
                parent_rank[id(flat_idx[k][1])] = i_use + 0.3
                if len(parents) >= 4:
                    break
        if parents:
            use = use + parents
            steps.append({"name": "상위 조문 자동 포함",
                          "detail": ", ".join(_ctx_key(f, a) for f, a in parents)})

    # ── 인용된 별표를 자동으로 끌어옵니다 ─────────────────────────
    # "누출검사주기는 별표 4와 같다" 처럼 조문이 별표에 넘기는 경우,
    # 별표를 안 가져오면 AI 가 숫자를 지어내거나 조문에 없는 내용을 붙입니다.
    def _bp_no(a):
        """별표 항목에서 번호를 뽑습니다. 조문제목이 '[별표 4] …' 형태입니다."""
        # ★ 2026-10-02 — "별표 3의2" 의 가지번호까지 읽습니다. 안 읽으면 별표 3 과 섞입니다.
        mt = re.match(r"\[(별표|별지|서식)\s*(\d+)(?:의(\d+))?", a.get("조문제목", ""))
        return (mt.group(1), mt.group(2) + (f"의{mt.group(3)}" if mt.group(3) else "")) if mt else None

    # ★ 2026-08-19 — 별표 번호를 **인용한 조문이 속한 법령**에 무조건 붙이고
    #   있었습니다. 그런데 조문은 다른 법령의 별표도 인용합니다.
    #     「토양환경보전법 시행규칙」 제N조 … "「폐기물관리법 시행규칙」 별표 5에 따른"
    #   그러면 토양환경보전법 시행규칙의 별표 5(전혀 다른 내용)를 끌어와 AI 에
    #   넣거나, 없으면 "「토양환경보전법 시행규칙」 별표 5 를 못 받았다" 는
    #   엉뚱한 경고를 띄웠습니다.
    #   → 별표 앞 100자 안에 「다른 법령명」이 있으면 그 법령으로 붙입니다.
    #     수집한 법령 목록에 없는 이름이면 아예 건드리지 않습니다.
    _known = {f["name"] for f in found}
    wanted = set()
    # ★ v1.32 — 우선순위(작을수록 중요). 선별 순서 그대로이고, 자동으로 끌어온 별표는
    #   **그 별표를 인용한 조문 바로 앞** 순위를 받습니다. 토큰 예산을 넘으면 큰 순위부터 뺍니다.
    #   (v1.31: 별표를 무조건 맨 나중에 빼서, 덜 중요한 조문이 인용한 큰 별표가 남고
    #    핵심 조문이 빠지는 일이 있었음)
    prio = {id(a): float(i) for i, (f, a) in enumerate(use)}
    prio.update(parent_rank)
    cite_rank: dict = {}
    for i_use, (f, a) in enumerate(use):
        body = a.get("조문내용", "")
        for mt in re.finditer(r"(별표|별지)\s*제?\s*(\d+)(?:\s*의\s*(\d+))?\s*호?", body):
            kind = mt.group(1)
            no = mt.group(2) + (f"의{mt.group(3)}" if mt.group(3) else "")
            owner = f["name"]
            near = body[max(0, mt.start() - 100):mt.start()]
            names = re.findall(r"「([^」]{2,60})」", near)
            if names:
                cand = names[-1].strip()
                if cand in _known:
                    owner = cand
                elif cand != f["name"]:
                    continue          # 우리가 안 가진 법령의 별표 — 건드리지 않음
            wanted.add((owner, kind, no))
            cite_rank.setdefault((owner, kind, no), i_use)
    # 선별이 직접 고른 별표도, 그 별표를 인용한 조문이 더 앞에 있으면 그 순위를 따릅니다.
    for f, a in use:
        k = _bp_no(a)
        if k and (f["name"], *k) in cite_rank:
            prio[id(a)] = min(prio[id(a)], cite_rank[(f["name"], *k)] - 0.5)

    # 본문을 끝내 확보하지 못한 별표. 답변 검증 단계에서 경고를 띄우는 데 씁니다.
    missing_bp: list[dict] = []

    if wanted:
        already = {(f["name"], *(_bp_no(a) or ("", "")))for f, a in use}
        added = []
        for f, a in flat:
            key = _bp_no(a)
            if not key:
                continue
            if (f["name"], key[0], key[1]) in wanted and (f["name"], *key) not in already:
                added.append((f, a))
        if added:
            use = use + added
            for f, a in added:
                k = _bp_no(a)
                prio[id(a)] = cite_rank.get((f["name"], *k), len(use)) - 0.5
            steps.append({"name": "별표 자동 포함",
                          "detail": ", ".join(a.get("조문제목", "")[:30] for _, a in added[:5])})

        # ── 본문 조회로도 안 잡힌 별표는 별표·서식 API 로 한 번 더 ──────
        # 법령(target=law)은 본문 조회에 별표가 아예 딸려오지 않습니다.
        # 이것이 "별표 4와 같다" 만 보고 AI 가 주기를 지어내던 원인입니다.
        got = already | {(f["name"], *(_bp_no(a) or ("", ""))) for f, a in added}
        still = sorted(w for w in wanted if w not in got)
        if still:
            by_name = {}
            for f, _ in use:
                by_name.setdefault(f["name"], f)
            fetched = []
            for law_name, kind, no in still:
                f = by_name.get(law_name)
                if not f:
                    continue
                try:
                    rows = law_client.get_byeolpyo(
                        f.get("target", "law"), f.get("mst") or f.get("id", ""), law_name)
                except Exception:                 # 별표는 보조 정보. 실패해도 조회는 계속합니다
                    rows = []
                row = next((r for r in rows
                            if (r.get("no", "") + (f"의{r['gaji']}" if r.get("gaji") else "")) == no
                            and (r.get("kind") or "별표") == kind), None)
                link = (row or {}).get("link", "")
                title = (row or {}).get("title", "")
                if row and row.get("body"):
                    body = row["body"][:12000]    # v1.32: 6천 → 1만2천 (비고까지 받도록; 넘길 때 압축)
                    fetched.append(f"{law_name} {kind} {no}")
                elif kind == "별지":
                    # 별지는 신고서·신청서 같은 **서식**입니다. 주기·수치를 정하지
                    # 않으므로 본문이 없어도 답변 근거에 지장이 없습니다.
                    # 경고를 띄우면 진짜 경고(별표)까지 같이 무시하게 되므로
                    # missing_bp 에 넣지 않습니다. 링크만 남깁니다.
                    body = (f"[{kind} {no}] {title}\n"
                            f"(제출용 서식입니다. 양식 자체는 주기·기준을 정하지 않습니다.)")
                else:
                    # ★ 본문을 못 받았다는 사실을 컨텍스트에 명시적으로 넣습니다.
                    #   비워두면 AI 는 별표가 없다는 것조차 모르고 기억으로 채웁니다.
                    #   제목은 받았으므로 "무엇을 정하는 별표인지" 는 알려줍니다.
                    #   그래야 "이 별표를 봐야 한다" 고 정확히 안내할 수 있습니다.
                    body = (f"[{kind} {no}] {title}\n"
                            f"({kind} {no} 의 제목은 위와 같습니다. 그러나 본문은 법제처가 "
                            f"API 로 제공하지 않아 가져오지 못했습니다. "
                            f"이 {kind} 가 정한 주기·기간·수치·기준은 확인할 수 없습니다. "
                            f"절대 추측하거나 기억으로 채우지 말고, "
                            f"'{kind} {no}({title}) 원문을 확인해야 한다' 고 답하십시오.)")
                    missing_bp.append({"law": law_name, "kind": kind, "no": no,
                                       "title": title, "link": link})
                art = {
                    "조문번호": "", "조문가지번호": "",
                    "조문제목": f"[{kind} {no}] {title}".strip(),
                    "조문여부": "조문", "시행일자": "", "구분": kind,
                    "조문내용": body, "파일링크": link,
                }
                # ★ 2026-08-19 — `f` 는 캐시에 들어 있는 바로 그 dict 입니다.
                #   그냥 append 하면 되묻기 라운드마다(같은 캐시로 다시 들어올
                #   때마다) 같은 별표가 계속 쌓여, 화면과 AI 컨텍스트에 중복으로
                #   들어갑니다. 같은 제목이 이미 있으면 붙이지 않습니다.
                arts = f.setdefault("articles", [])
                if not any(x.get("조문제목") == art["조문제목"] for x in arts):
                    arts.append(art)
                use.append((f, art))
                prio[id(art)] = cite_rank.get((law_name, kind, no), len(use)) - 0.5
            if fetched:
                steps.append({"name": "별표 API 조회", "detail": ", ".join(fetched)})
            if missing_bp:
                steps.append({
                    "name": "별표 본문 미확보",
                    "detail": ", ".join(f"{m['law']} {m['kind']} {m['no']}" for m in missing_bp)
                              + " — 첨부파일로만 제공되어 수치 확인 불가",
                })

    # ── 조문 본문 렌더링 (use 순서 = 우선순위) ───────────────────
    delegated_hits = 0     # 위임법령 힌트를 실제로 붙인 조문 수 (처리 과정 표시용)
    units = []             # (법령, 조문, 컨텍스트에 들어갈 텍스트)
    for f, a in use:
        body = a.get("조문내용", "")
        if a.get("구분") in ("별표", "별지", "서식") or \
                str(a.get("조문제목", "")).startswith(("[별표", "[별지", "[서식")):
            # v1.32: 원문 2천 자 자르기 → 테두리·공백 압축 후 비고를 남기며 자르기
            body = _fit_keep_notes(_compact_bp(body), BP_CTX_CHARS)

        # ★ 위임법령(lsDelegated) 힌트 — 이 조문이 위임한 하위법령 조문번호를
        #   법제처 데이터로 못박아 둡니다. AI가 시행령·시행규칙 조문번호를
        #   짐작해서 틀리는 것을 막으려는 목적이라, 조문 개수가 많아도
        #   법률 조문에만(연결이 있을 때만) 붙습니다.
        #   ★ 2026-08-18 — 위임 대상은 종류마다 필드 이름이 다르고
        #     (위임법령제목 / 위임행정규칙제목 / 위임자치법규제목 …),
        #     행정규칙·자치법규는 조문번호를 아예 주지 않습니다.
        #     law_client.get_delegated() 가 _kind_raw / _title / _jo 로
        #     정규화해 주므로 그것을 씁니다.
        #   ★ `인용법령` 은 위임이 아니라 단순 상호참조입니다. 실측상
        #     건수가 압도적(455건 중 354건)이라 문장을 나눠 씁니다 —
        #     한 덩어리로 "위임됩니다" 라고 쓰면 AI가 상호참조를
        #     하위법령으로 오해합니다.
        dele = (f.get("delegated") or {}).get(
            law_client.dele_key(a.get("조문번호", ""), a.get("조문가지번호", "")))
        if dele:
            def _cite(d):
                title = d.get("_title") or d.get("위임법령제목", "")
                if not title:
                    return ""
                jo = (d.get("_jo") or "").lstrip("0")
                gaji = (d.get("_jo_gaji") or "").lstrip("0")
                if jo:
                    return f"「{title}」 제{jo}조" + (f"의{gaji}" if gaji else "")
                # 행정규칙·자치법규·규정·조약 — 조문번호 없이 이름만 옵니다.
                # 조문번호를 지어내지 못하게 "미제공" 이라고 못박습니다.
                return f"「{title}」({d.get('_kind_raw') or d.get('_kind') or '위임'}, 조문번호 미제공)"

            # 위임(시행령·시행규칙·고시…) 과 인용(상호참조) 을 갈라 담습니다.
            # get_delegated() 가 위임을 앞으로 정렬해 주므로 앞에서 자릅니다.
            dele_hints, ref_hints = [], []
            for d in dele:
                c = _cite(d)
                if not c:
                    continue
                if d.get("_kind_raw") == "인용법령":
                    if len(ref_hints) < 3 and c not in ref_hints:
                        ref_hints.append(c)
                elif len(dele_hints) < 4 and c not in dele_hints:
                    dele_hints.append(c)

            note = ""
            if dele_hints:
                note += ("\n(※ 법제처 위임법령 데이터: 이 조문은 "
                         + ", ".join(dele_hints) +
                         "에 위임됩니다. 하위법령을 인용할 때는 위 이름과 "
                         "조문번호를 그대로 쓰고, 다른 번호를 짐작해서 쓰지 "
                         "마십시오. '조문번호 미제공' 이라고 적힌 것은 "
                         "조문번호를 빼고 이름만 쓰십시오.")
            if ref_hints:
                note += (("\n(※ " if not note else " 또한 ")
                         + "법제처 데이터상 이 조문이 참조하는 조문: "
                         + ", ".join(ref_hints)
                         + " — 이것은 위임이 아니라 상호참조이므로 "
                           "'하위법령' 이라고 쓰지 마십시오.")
            if note:
                body += note + ")"
                delegated_hits += 1

        # ★ 조문마다 법령명을 앞에 붙입니다.
        #   구분선만 두면 AI 가 아래로 내려갈수록 어느 법령인지 잊고
        #   법률 제8조를 "시행령 제8조" 로 인용하는 오류가 납니다.
        units.append((f, a, f"[{f['name']}] {label_of(a)}\n{body}"))
    if delegated_hits:
        steps.append({"name": "위임법령 힌트 추가",
                      "detail": f"법률 조문 {delegated_hits}개에 위임 조문번호 힌트 붙임"})

    def _is_bp(u):
        a = u[1]
        return a.get("구분") in ("별표", "별지", "서식") or \
            str(a.get("조문제목", "")).startswith(("[별표", "[별지", "[서식"))

    # --- 3-2단계: 되묻기 (v1.31: 선별·별표 보강 **뒤**, 조문 **본문** 기반) ----
    # ★ 2026-10-07 — v1.30 까지는 선별 **전에** 조문 **제목** 60줄만 보고 물었습니다.
    #   · catalog 는 법령 순서로 나열돼 두 번째 법령·시행규칙·별표는 아예 안 보였고
    #   · 기준값(2만L·1년·3회)은 본문·별표에 있어 보기를 나눌 수 없었습니다.
    #   이제 선별된 조문과 별표 본문을 토큰 상한(CLARIFY_CTX_TOKENS) 안에서 넘깁니다.
    # ★ 의도가 사용자 사실을 요구하지 않으면(조문 조회·정의·일반 기준) 묻지 않습니다.
    if not req.skip_clarify and req.round == 0 and not it.needs_facts:
        steps.append({"name": "되묻기 생략",
                      "detail": f"질문 유형 '{it.label}' — 사례 사실 없이 답할 수 있음"})
    # ★ v1.32 — 질문에 구체적 사실(수치·단위·날짜)이 2개 이상 적혀 있으면 되묻기는 1라운드까지만.
    #   사실을 충분히 준 질문에 라운드를 거듭하면 지엽적인 것(측정 기관·300m 이내 주택)을 묻고,
    #   그 "모름" 때문에 답변까지 흐려졌습니다(공사장 소음 68dB 사례).
    n_facts = len(intent_mod._CASE_FACTS.findall(req.question))
    max_r = min(MAX_ROUNDS, 1) if n_facts >= 2 else MAX_ROUNDS
    if LAW_Q in (answered or ""):
        max_r += 1                     # v1.33 — "어느 법" 라운드는 사실 확인 라운드에서 빼고 셉니다
    if not req.skip_clarify and req.round < max_r and it.needs_facts:
        prim = [u for u in units if not _is_bp(u)]
        bps = [u for u in units if _is_bp(u)]
        order = prim[:8] + bps[:4] + prim[8:] + bps[4:]
        parts, used = [], 0
        for f, a, txt in order:
            # 별표는 비고(보정·예외)가 보기를 가르므로 조금 더 길게, 비고를 남겨 자릅니다.
            piece = _fit_keep_notes(txt, 2500 if _is_bp((f, a, txt)) else 1500)
            if used + len(piece) > CLARIFY_CTX_TOKENS * 2 and parts:   # 글자 기준 1차 컷
                break
            parts.append(piece)
            used += len(piece)
        clar_ctx = "\n\n".join(parts)
        n_cl = ai_client.count_tokens(clar_ctx)
        if n_cl > CLARIFY_CTX_TOKENS:                                  # 토큰 기준 2차 컷
            clar_ctx = clar_ctx[:int(len(clar_ctx) * CLARIFY_CTX_TOKENS / n_cl * 0.95)] \
                + "\n…(분량 제한으로 이하 생략)"
        try:
            asks = ai_client.clarify(req.question, answered, clar_ctx)
        except ai_client.AiError as e:
            applog.warn(f"되묻기 판단 실패 — 건너뜁니다: {e}")
            asks = []          # 판단 실패는 그냥 통과시킵니다

        if asks:
            # 이미 물은 질문(모름으로 답한 것 포함)과 겹치면 버립니다.
            # 프롬프트 지시를 로컬 모델이 무시해도 여기서 최종적으로 막힙니다.
            already = _asked_questions(answered)
            fresh = [a for a in asks if not _is_repeat_question(a["question"], already)]
            if not fresh:
                applog.debug("clarify", f"이미 물은 질문 {len(asks)}개 반복 감지 → 되묻기 종료")
                steps.append({"name": "되묻기 종료",
                              "detail": "같은 질문이 반복돼 다음 단계로 진행"})
            asks = fresh

        if not asks:
            steps.append({"name": "되묻기 판단",
                          "detail": f"더 물을 것 없음 (조문 {len(parts)}개 본문 기준)"})
        if asks:
            steps.append({"name": "질문 확인",
                          "detail": f"{req.round + 1}차 · {len(asks)}개 항목 "
                                    f"(조문 {len(parts)}개 본문 기준)"})
            return {
                "clarify": asks,
                "round": req.round + 1,
                "max_rounds": max_r,
                "answered": answered,
                "steps": steps,
                "intent": it.as_dict(),
            }

    def _assemble(sel):
        """법령별로 묶어 컨텍스트 텍스트를 만듭니다 (법령 순서는 use 에 처음 나온 순서)."""
        by_law = {}
        for f, a, txt in sel:
            by_law.setdefault(id(f), (f, []))[1].append(txt)
        # ★ 조문마다 법령명을 앞에 붙입니다(위 txt). 구분선만 두면 AI 가 아래로
        #   내려갈수록 어느 법령인지 잊고 법률 제8조를 "시행령 제8조" 로 인용합니다.
        return "\n\n".join(
            f"=== 여기부터는 「{f['name']}」 조문입니다 "
            f"(시행 {f.get('enforced', '?')}) ===\n" + "\n\n".join(txts)
            for f, txts in by_law.values()), len(by_law)

    # ── 토큰 예산 ────────────────────────────────────────────────
    # ★ 2026-09-29 — 로컬 LLM 컨텍스트 = 지시문 + 질문 + 조문 + **답변(출력)**.
    #   예산을 넘으면 중간을 뚝 자르지 않고, 우선순위가 낮은 조문부터 통째로 뺍니다.
    #   우선순위: 조문 선별이 고른 순서(선별 프롬프트가 중요한 것부터 적게 함).
    #   별표는 수치가 들어 있어 맨 나중에 뺍니다. 최소 3개는 남깁니다.
    _hint = {
        intent_mod.DEFINITION: "\n(질문 유형: 용어 정의 — 정의 조문을 먼저 인용하고 쉽게 풀어 설명한다)",
        intent_mod.GENERAL_RULE: "\n(질문 유형: 일반 기준·절차 설명 — 특정 사례를 판정하는 질문이 아니다. "
                                 "조건에 따라 기준이 달라지면 경우를 나눠 정리한다)",
    }.get(it.kind, "")
    q_for_answer = req.question + (f"\n(확인된 조건: {answered})" if answered else "") + _hint
    ctx_n = ai_client.server_ctx()
    overhead = ai_client.count_tokens(
        ai_client.ANSWER_PROMPT.format(question=q_for_answer, context=""))
    budget = ctx_n - overhead - min(ai_client.MAXTOK_ANSWER, ANSWER_RESERVE) - 96

    keep = list(units)
    context, n_laws = _assemble(keep)
    n_tok = ai_client.count_tokens(context)
    dropped = []
    if n_tok > budget:
        # ★ v1.32 — 조문을 빼기 **전에** 별표를 먼저 줄입니다(비고는 남김).
        #   큰 별표(시설·장비 기준표 등) 몇 개가 예산을 다 먹어 핵심 조문이 빠지는 것을 막습니다.
        shrunk = 0
        for j, u in enumerate(keep):
            if _is_bp(u) and len(u[2]) > 1800:
                keep[j] = (u[0], u[1], _fit_keep_notes(u[2], 1800))
                shrunk += 1
        if shrunk:
            before_tok = n_tok
            context, n_laws = _assemble(keep)
            n_tok = ai_client.count_tokens(context)
            steps.append({"name": "별표 축약",
                          "detail": f"예산 {budget:,}토큰 초과 — 별표 {shrunk}개를 1,800자로 줄임(비고 유지) "
                                    f"{before_tok:,} → {n_tok:,}토큰"})
    if n_tok > budget:
        # 우선순위가 낮은(prio 가 큰) 것부터 뺍니다. 같은 순위면 뒤에 있는 것부터.
        pos = {id(u[1]): i for i, u in enumerate(keep)}
        order = sorted(keep, key=lambda u: (-prio.get(id(u[1]), float(len(keep))), -pos[id(u[1])]))
        for _ in range(5):
            ratio = n_tok / max(1, len(context))          # 글자당 토큰 (실측)
            need = (n_tok - budget) * 1.1
            while need > 0 and order and len(keep) > 3:
                u = order.pop(0)
                keep.remove(u)
                dropped.append(u)
                need -= len(u[2]) * ratio
            context, n_laws = _assemble(keep)
            n_tok = ai_client.count_tokens(context)
            if n_tok <= budget or len(keep) <= 3 or not order:
                break
        if n_tok > budget:                                # 최후 수단: 글자 단위로 자름
            cut = int(len(context) * budget / n_tok * 0.95)
            context = context[:cut] + "\n…(분량 제한으로 이하 생략)"
            n_tok = ai_client.count_tokens(context)
        if dropped:
            names = [f"「{f['name']}」 {label_of(a)}" for f, a, _ in dropped]
            # 빠진 조문이 있다는 사실을 모델에게 알립니다. 모르면 그 내용을 기억으로 채웁니다.
            context += ("\n\n(※ 분량 제한으로 다음 조문은 넣지 못했습니다: " + ", ".join(names[:15])
                        + (" 외" if len(names) > 15 else "")
                        + ". 이 조문의 내용이 필요하면 추측하지 말고 원문 확인이 필요하다고 답하십시오.)")
            steps.append({"name": "조문 분량 조절",
                          "detail": f"예산 {budget:,}토큰 초과 — {len(dropped)}개 제외: "
                                    + ", ".join(names[:5]) + (" 외" if len(names) > 5 else "")})
    steps.append({
        "name": "조문 수집",
        "detail": f"{n_laws}개 법령 / 조문 {len(keep)}개 / {n_tok:,}토큰 "
                  f"(예산 {budget:,}, 컨텍스트 {ctx_n:,})",
    })
    if not context.strip():
        return {"steps": steps, "answer": "조문 본문을 가져오지 못했습니다.", "laws": found}

    # --- 4단계: 답변 + 검증 ---------------------------------------
    try:
        text = ai_client.answer(q_for_answer, context)
    except ai_client.AiError as e:
        return _err(str(e))

    cites = verify_citations(text, found)
    steps.append({"name": "인용 검증", "detail": f"{sum(1 for c in cites if c['ok'])}/{len(cites)}건 확인"})

    # ── 안전장치: 본문 없는 별표를 답변이 **근거로 썼는지** ────────
    # 실제 사고: 별표 4 본문이 없는데 모델이 "매 8년" 이라고 했다가
    # 같은 질문에 다시 "5·10·15년 이후 매 2년" 이라고 했습니다. 둘 다 근거가 없습니다.
    # 담당자가 이 수치를 민원인에게 그대로 안내하면 사고로 이어집니다.
    warnings = []
    if missing_bp:
        cited_bp = _bp_used_as_basis(text)
        hits = [m for m in missing_bp if (m["kind"], m["no"]) in cited_bp]
        if hits:
            names = ", ".join(
                f"「{m['law']}」 {m['kind']} {m['no']}"
                + (f"({m['title']})" if m.get("title") else "")
                for m in hits)
            warnings.append(
                f"답변이 {names} 을(를) 근거로 들었으나, 이 별표의 본문은 법제처가 "
                f"API 로 제공하지 않아 가져오지 못했습니다. "
                f"답변에 적힌 주기·기간·수치는 확인된 근거가 없으므로 그대로 사용하지 마시고 "
                f"아래 주소에서 원문을 직접 확인하세요."
            )
            for m in hits:
                if m.get("link"):
                    warnings.append(f"{m['kind']} {m['no']} 원문 보기: {m['link']}")
            steps.append({"name": "별표 경고", "detail": names + " — 근거 없는 수치 경고 표시"})

    # ── 안전장치: 답변이 날짜를 계산했으면 코드가 검산합니다 ─────────
    calc_warn = verify_calc(text, f"{req.question}\n{answered}")
    if calc_warn:
        warnings.extend(calc_warn)
        steps.append({"name": "계산 검산", "detail": f"{len(calc_warn)}건 이상 발견"})

    # 인용된 조문 번호 집합 — 화면에서 이것만 펼쳐 보여줍니다.
    # 법령별로 어떤 조문이 인용됐는지. 법률 제3조와 시행령 제3조를 구분하기 위해
    # 법령명까지 키에 넣습니다.
    hit = sorted({(c["law"], c["jo"], c["gaji"]) for c in cites if c["ok"]})

    return {
        "steps": steps, "answer": text, "laws": found, "citations": cites,
        "cited_keys": [f"{n}|{j}|{g}" for n, j, g in hit],
        "references": [r for r, _ in sorted(refs.items(), key=lambda x: -x[1])][:12],
        "warnings": warnings,
        "intent": it.as_dict(),
        # v1.31 — 평가용: 컨텍스트에 실제로 들어간 조문(검색 Recall 측정), 빠진 조문
        "debug": {
            "flat_n": len(flat),
            "picked_n": len(picked or []),
            "context_keys": [_ctx_key(f, a) for f, a, _ in keep],
            "dropped_keys": [_ctx_key(f, a) for f, a, _ in dropped],
            "user_laws": _user_laws(req),
            "laws_mode": _laws_mode(req),
            # v1.32 — 지능형 검색이 돌려준 조문 순서(법령 순위 판단을 나중에 검토하려고)
            "ai_hits": [f"{n} 제{str(j).lstrip('0')}조" + (f"의{str(g).lstrip('0')}" if str(g).strip("0") else "")
                        for n, j, g in (ai_hits or [])[:20]],
        },
    }
