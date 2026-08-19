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
from datetime import datetime
import logging
import os
import secrets

# 서버 로그 앞에 시각(연-월-일 시:분:초)을 붙입니다.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
for _n in ("uvicorn", "uvicorn.access", "uvicorn.error"):
    _lg = logging.getLogger(_n)
    _lg.handlers.clear()          # uvicorn 기본 포맷을 제거하고
    _lg.propagate = True          # 위 설정을 따르게 합니다

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
import law_client  # noqa: E402
import pdf_maker  # noqa: E402

# 되묻기 최대 라운드. .env 의 CLARIFY_ROUNDS 로 조절합니다.
# 라운드마다 Gemini 호출이 1회 늘어납니다. 무료 한도가 넉넉해지면 올리세요.
CLARIFY_ROUNDS = int(os.getenv("CLARIFY_ROUNDS", "6"))

# 되묻기 판단에 넘길 조문 목록 줄 수.
# 로컬 모델은 입력이 길수록 급격히 느려지므로 앞부분만 보여줍니다.
CLARIFY_CATALOG_LINES = int(os.getenv("CLARIFY_CATALOG_LINES", "60"))

# 답변 생성에 넣을 조문 최대 글자 수. 이것이 토큰 사용량을 좌우합니다.
# 무료 등급은 분당 토큰(TPM)이 병목이라, 이 값을 줄이는 것이 가장 효과가 큽니다.
CONTEXT_LIMIT = int(os.getenv("CONTEXT_LIMIT", "60000"))

# 조문 선별 사용 여부. 0 이면 예전처럼 전체 조문을 넣습니다.
SELECT_ARTICLES = os.getenv("SELECT_ARTICLES", "1") not in ("0", "false", "False")

# 자치법규(조례·규칙)를 찾을 지자체.
# 법제처 조례 검색은 다른 지자체 결과까지 섞어 돌려주므로,
# 조회 후 지자체기관명으로 한 번 더 걸러냅니다.
LOCAL_GOV = os.getenv("LOCAL_GOV", "성남시").strip()

VERSION = "1.26"

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
#   산수는 프롬프트로 고쳐지지 않습니다. 9B 모델에게 연도 덧셈을 정확히
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

    # (1) 산수 검산 — 블록에 적힌 주기 중 하나라도 맞으면 통과
    ok = False
    for n_s, unit in periods:
        try:
            n = int(n_s)
        except ValueError:
            continue
        if _add_period(by, bm, bd, n, unit) == (ry, rm, rd):
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
    if stamp not in src and loose not in src:
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
    return False


TARGET_LABEL = {"law": "법령", "eflaw": "법령", "admrul": "행정규칙",
                "ordin": "자치법규", "expc": "법령해석례"}


def _err(msg: str):
    """
    앱 오류 응답.

    ★ 상태 코드를 200 으로 둡니다.
      Cloudflare 등 프록시가 5xx 응답 본문을 자체 HTML 오류 페이지로 교체해버려,
      실제 오류 메시지가 사용자에게 전달되지 않기 때문입니다.
      화면은 상태 코드가 아니라 error/quota 필드를 보고 판단합니다.
    """
    return JSONResponse({"error": msg}, status_code=200)


def _quota_response(e):
    """무료 한도 초과를 화면 팝업용 형식으로 돌려줍니다."""
    if e.scope == "day":
        title, msg = "오늘 사용 한도를 다 썼습니다", "무료 한도는 하루 단위로 초기화됩니다. 내일 다시 이용해 주세요."
    elif e.scope == "minute":
        title, msg = "잠시만 기다려 주세요", f"짧은 시간에 요청이 몰렸습니다. 약 {e.retry_after}초 뒤에 다시 시도해 주세요."
    else:
        title, msg = "사용 한도에 도달했습니다", "분당 한도인지 일일 한도인지 확인되지 않았습니다. 5분 뒤에 다시 시도해 보시고, 그래도 안 되면 내일 이용해 주세요."
    return JSONResponse(
        {"quota": {"title": title, "message": msg, "scope": e.scope, "retry_after": e.retry_after}},
        status_code=200,
    )


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
    """화면과 서버 버전, 인증키 상태."""
    return {
        "version": VERSION,
        "keys": ai_client.key_status(),
        "models": {
            "terms": ai_client.MODEL_TERMS,
            "clarify": ai_client.MODEL_CLARIFY,
            "answer": ai_client.MODEL_ANSWER,
        },
        "backends": {
            "terms": ai_client.BACKEND_TERMS,
            "select": ai_client.BACKEND_SELECT,
            "clarify": ai_client.BACKEND_CLARIFY,
            "answer": ai_client.BACKEND_ANSWER,
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

    progress: list = []          # _ask_sync 가 단계마다 채웁니다

    async def gen():
        loop = asyncio.get_running_loop()
        task = loop.run_in_executor(None, _ask_sync, req, progress)
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

        # ★ 2026-08-19 — _ask_sync 는 오류·한도 경로에서 JSONResponse **객체**를
        #   돌려줍니다(_err / _quota_response). 이 엔드포인트는 예전에는 그것을
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


def _ask_sync(req: AskRequest, progress: list):
    """
    질문 → (갈래 판단) → 용어변환 → 체계도 기반 계층 검색 → 조문 → 답변 → 검증
    """
    steps = progress          # 화면으로 실시간 전달되는 목록

    # 사용자가 직접 적은 내용을 조건에 합칩니다.
    answered = " / ".join(x for x in (req.answered, req.note.strip()) if x)

    # 되묻기 라운드가 넘어갈 때마다 아래 1~3단계를 다시 실행하고 있었습니다.
    # 검색 결과는 되묻기 답변과 무관하게 같으므로(질문·대상이 같으면 같은 법령),
    # 조건은 키에서 뺍니다. 조건은 답변 생성에만 쓰입니다.
    cache_key = (req.question.strip(), req.target)
    # ★ 새 질문(round=0)이면 캐시를 쓰지 않고 반드시 새로 검색합니다.
    #   되묻기 라운드 중(round>0)에만 재사용합니다.
    #   이것이 없으면 이전 질문의 법령이 그대로 남아 엉뚱한 답이 나옵니다.
    cached = _cache_get(cache_key) if req.round > 0 else None
    if req.round == 0:
        _SEARCH_CACHE.pop(cache_key, None)
    if cached:
        found, flat, catalog, refs = cached
        steps.append({"name": "이전 검색 재사용",
                      "detail": f"{len(found)}개 법령 / 조문 {len(flat)}개"})
        return _ask_after_search(req, steps, answered, found, flat, catalog, refs)

    # --- 1단계: 법령 용어로 변환 ----------------------------------
    try:
        terms = ai_client.extract_terms(req.question + (f"\n(조건: {answered})" if answered else ""))
    except ai_client.QuotaError as e:
        return _quota_response(e)
    except ai_client.AiError as e:
        return _err(str(e))
    steps.append({"name": "검색어 변환", "detail": terms})

    names = terms.get("법령명", [])
    words = terms.get("용어", [])
    if not names and not words:
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
        cand = list(names[:2])
        if cand:
            hit = False
            for nm in cand:
                base = nm.replace(" 시행령", "").replace(" 시행규칙", "").strip()
                try:
                    if law_client.search("lsStmd", base):
                        hit = True
                        break
                except law_client.LawApiError:
                    pass

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
                        if law_client.search("lsStmd", full):
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

            if not hit and words:
                recovered = []
                for w in words[:3]:
                    try:
                        rows = law_client.search(LAW_T, w, scope=2)   # 본문 검색
                    except law_client.LawApiError:
                        continue
                    for r in rows[:3]:
                        nm = law_client.row_name(LAW_T, r)
                        # 시행령·시행규칙은 체계도가 알아서 따라옵니다. 본법만 모읍니다.
                        nm = re.sub(r"\s*(시행령|시행규칙)$", "", nm).strip()
                        if nm and nm not in recovered and nm not in cand:
                            recovered.append(nm)
                if recovered:
                    steps.append({
                        "name": "법령명 복구(본문 검색)",
                        "detail": f"'{', '.join(cand)}' 로 체계도를 찾지 못해 "
                                  f"'{', '.join(words[:3])}' 본문 검색 → "
                                  + ", ".join(recovered[:3]),
                    })
                    cand = recovered[:2] + cand

        # v1.3 의 조문 선별이 붙어 토큰 부담이 크게 줄었으므로 후보를 2개로 되돌립니다.
        # 1개만 쓰면 "누출검사" 처럼 여러 법에 쓰이는 용어에서 엉뚱한 법 하나만
        # 잡고 끝나 답이 통째로 틀립니다. (토양환경보전법 → 위험물안전관리법)
        for nm in cand[:2]:
            base = nm.replace(" 시행령", "").replace(" 시행규칙", "").strip()
            try:
                rows = law_client.search("lsStmd", base)
            except law_client.LawApiError:
                rows = []
            if not rows:
                continue
            mst = law_client.pick(rows[0], "법령일련번호")
            if not mst:
                continue
            try:
                tree = law_client.get_hierarchy(mst)
            except law_client.LawApiError:
                continue
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
    if req.target in ("auto", "ordin"):
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

    _cache_put(cache_key, (found, flat, catalog, refs))
    return _ask_after_search(req, steps, answered, found, flat, catalog, refs)


def _ask_after_search(req, steps, answered, found, flat, catalog, refs):
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

    # --- 3-0단계: 갈래 판단 (되묻기) ------------------------------
    # ★ 되묻기는 조문을 확보한 뒤에 합니다.
    #   조문을 보기 전에 물으면 AI 가 기억에 의존해 존재하지 않는 용어를 지어냅니다.
    #   (실제 사례: "특정토양오염유발시설" — 법령에 없는 이름,
    #    "VOC 배출시설" — 다른 법 소관인데 선택지로 등장)
    MAX_ROUNDS = CLARIFY_ROUNDS
    if not req.skip_clarify and req.round < MAX_ROUNDS:
        # 되묻기에는 조문 목록을 통째로 넣지 않습니다.
        # 수백 줄을 넣으면 로컬 모델이 몇 분씩 걸립니다.
        # 갈래 판단에는 어떤 법령의 어떤 조문이 있는지 정도면 충분합니다.
        brief = "\n".join(catalog.splitlines()[:CLARIFY_CATALOG_LINES])
        if len(catalog.splitlines()) > CLARIFY_CATALOG_LINES:
            brief += f"\n… (외 {len(catalog.splitlines()) - CLARIFY_CATALOG_LINES}개)"
        try:
            asks = ai_client.clarify(req.question, answered, brief)
        except ai_client.QuotaError as e:
            return _quota_response(e)
        except ai_client.AiError:
            asks = []          # 판단 실패는 그냥 통과시킵니다

        if asks:
            # 이미 물은 질문(모름으로 답한 것 포함)과 겹치면 버립니다.
            # 프롬프트 지시를 로컬 모델이 무시해도 여기서 최종적으로 막힙니다.
            already = _asked_questions(answered)
            fresh = [a for a in asks if not _is_repeat_question(a["question"], already)]
            if not fresh:
                if ai_client.LLM_DEBUG:
                    print(f"[clarify] 이미 물은 질문 {len(asks)}개 반복 감지 → 되묻기 종료",
                          flush=True)
                steps.append({"name": "되묻기 종료",
                              "detail": "같은 질문이 반복돼 다음 단계로 진행"})
            asks = fresh

        if asks:
            steps.append({"name": "질문 확인",
                          "detail": f"{req.round + 1}차 · {len(asks)}개 항목"})
            return {
                "clarify": asks,
                "round": req.round + 1,
                "max_rounds": MAX_ROUNDS,
                "answered": answered,
                "steps": steps,
            }

    # --- 3-1단계: 필요한 조문만 고르기 ----------------------------
    # 조문 본문을 통째로 넣으면 요청당 3만 토큰. 제목만 보여주고 고르면 2천 토큰.
    picked = None
    if SELECT_ARTICLES and len(flat) > 12:
        try:
            nums = ai_client.select_articles(req.question, catalog)
            picked = [flat[n - 1] for n in nums if 1 <= n <= len(flat)]
        except ai_client.QuotaError as e:
            return _quota_response(e)
        except ai_client.AiError:
            picked = None                      # 실패하면 전체를 씁니다
        if picked:
            steps.append({"name": "조문 선별",
                          "detail": f"{len(flat)}개 중 {len(picked)}개 선택"})

    # ★ 아래에서 별표를 덧붙이므로 반드시 복사본으로 씁니다.
    #   picked/flat 을 그대로 쓰면 append 가 원본 목록을 오염시킵니다.
    use = list(picked or flat)

    # ── 인용된 별표를 자동으로 끌어옵니다 ─────────────────────────
    # "누출검사주기는 별표 4와 같다" 처럼 조문이 별표에 넘기는 경우,
    # 별표를 안 가져오면 AI 가 숫자를 지어내거나 조문에 없는 내용을 붙입니다.
    def _bp_no(a):
        """별표 항목에서 번호를 뽑습니다. 조문제목이 '[별표 4] …' 형태입니다."""
        mt = re.match(r"\[(별표|별지|서식)\s*(\d+)", a.get("조문제목", ""))
        return (mt.group(1), mt.group(2)) if mt else None

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
    for f, a in use:
        body = a.get("조문내용", "")
        for mt in re.finditer(r"(별표|별지)\s*제?\s*(\d+)\s*호?", body):
            kind, no = mt.group(1), mt.group(2)
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
                            if r.get("no") == no and (r.get("kind") or "별표") == kind), None)
                link = (row or {}).get("link", "")
                title = (row or {}).get("title", "")
                if row and row.get("body"):
                    body = row["body"][:6000]
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
            if fetched:
                steps.append({"name": "별표 API 조회", "detail": ", ".join(fetched)})
            if missing_bp:
                steps.append({
                    "name": "별표 본문 미확보",
                    "detail": ", ".join(f"{m['law']} {m['kind']} {m['no']}" for m in missing_bp)
                              + " — 첨부파일로만 제공되어 수치 확인 불가",
                })

    # 법령별로 다시 묶어 컨텍스트를 만듭니다.
    by_law = {}
    for f, a in use:
        by_law.setdefault(id(f), (f, []))[1].append(a)

    delegated_hits = 0     # 위임법령 힌트를 실제로 붙인 조문 수 (처리 과정 표시용)
    context_parts = []
    for f, arts in by_law.values():
        lines = []
        for a in arts:
            body = a.get("조문내용", "")
            if a.get("구분") in ("별표", "별지", "서식"):
                body = body[:2000]

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
            lines.append(f"[{f['name']}] {label_of(a)}\n{body}")
        context_parts.append(
            f"=== 여기부터는 「{f['name']}」 조문입니다 "
            f"(시행 {f.get('enforced', '?')}) ===\n" + "\n\n".join(lines)
        )
    if delegated_hits:
        steps.append({"name": "위임법령 힌트 추가",
                      "detail": f"법률 조문 {delegated_hits}개에 위임 조문번호 힌트 붙임"})

    context = "\n\n".join(context_parts)
    raw_len = len(context)
    if raw_len > CONTEXT_LIMIT:
        context = context[:CONTEXT_LIMIT] + "\n…(이하 생략)"
    steps.append({
        "name": "조문 수집",
        "detail": (f"{len(by_law)}개 법령 / {raw_len}자"
                   + (f" → {CONTEXT_LIMIT}자로 축소" if raw_len > CONTEXT_LIMIT else "")),
    })
    if not context.strip():
        return {"steps": steps, "answer": "조문 본문을 가져오지 못했습니다.", "laws": found}

    # --- 4단계: 답변 + 검증 ---------------------------------------
    try:
        text = ai_client.answer(
            req.question + (f"\n(확인된 조건: {answered})" if answered else ""), context)
    except ai_client.QuotaError as e:
        return _quota_response(e)
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
    }
