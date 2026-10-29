# -*- coding: utf-8 -*-
"""
법령 조회 도우미 — 자동 회귀 테스트 (평가 실행기)

무엇을 하는가
    시나리오 파일(eval_cases.json)에 적힌 질문을 실제 서버(/api/ask)에 넣고,
    되묻기가 나오면 **미리 정해 둔 답을 자동으로 골라** 다음 라운드로 넘기고,
    최종 답변을 기대값과 대조해 점수를 매깁니다.
    결과는 `_eval\\report_<시각>.md` 와 `.json` 두 벌로 남깁니다.

왜 브라우저가 아니라 API 인가
    화면을 자동화하면 **렌더된 글자만** 보입니다. 정작 진단에 필요한
    처리 단계(steps) · 인용 검증(citations) · 경고(warnings) 는 화면에
    다 안 나옵니다. API 를 직접 부르면 서버가 만든 것을 통째로 볼 수 있고,
    브라우저·확장 설치도 필요 없으며, 훨씬 빠르고 결과가 일정합니다.

쓰는 법
    (1) 서버를 켠 상태에서
            python eval_run.py
    (2) 특정 시나리오만
            python eval_run.py --only 누출검사주기
    (3) 다른 주소
            python eval_run.py --base http://127.0.0.1:8000

    표준 라이브러리만 씁니다. 설치할 것 없습니다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

# ★ 2026-08-20 — watch_and_test.py 가 이 파일을 서브프로세스로 돌리면 stdout 이
#   콘솔이 아니라 파이프로 리다이렉트됩니다. 그러면 Windows 에서 인코딩이
#   cp949(콘솔 코드페이지)로 잡혀서 ✅/❌/✗ 같은 문자를 못 쓰고 UnicodeEncodeError 로
#   죽었습니다 (5번째 시나리오에서 처음 실패 항목이 나오며 거기서 죽었습니다).
#   출력 인코딩을 강제로 UTF-8 로 고정합니다.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                 # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
CASES = os.path.join(HERE, "eval_cases.json")

# ★ 2026-08-20 — main.py 에 비밀번호 로그인(APP_PASSWORD)이 생긴 뒤로
#   /api/ask 가 전부 401 을 냈습니다. 로그인해서 받은 쿠키를 붙여야 합니다.
#   main.py 의 로그인 쿠키는 secure=True 라 urllib 의 자동 쿠키 처리(http.cookiejar)가
#   로컬 http 접속에는 다시 안 붙여줍니다(문서화된 제약). 그래서 자동 처리에
#   맡기지 않고, 로그인 응답의 Set-Cookie 값을 직접 읽어 매 요청에 수동으로 붙입니다.
_AUTH_COOKIE = ""


def _env_value(name: str) -> str:
    """.env 에서 값 하나를 읽습니다. (표준 라이브러리만 쓰므로 직접 파싱)"""
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == name:
                    return v.strip()
    except OSError:
        pass
    return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def login(base: str) -> str:
    """APP_PASSWORD 로 로그인해서 인증 쿠키 값을 얻습니다. 실패하면 빈 문자열."""
    password = _env_value("APP_PASSWORD")
    if not password:
        return ""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(
        base.rstrip("/") + "/login",
        data=urllib.parse.urlencode({"password": password}).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with opener.open(req, timeout=10) as resp:
            headers = resp.headers
    except urllib.error.HTTPError as e:
        headers = e.headers
    except Exception:                             # noqa: BLE001
        return ""
    for raw in headers.get_all("Set-Cookie") or []:
        if raw.startswith("lawfinder_auth="):
            return raw.split(";", 1)[0].split("=", 1)[1]
    return ""


OUTDIR = os.path.join(HERE, "_eval")


# =====================================================================
# 서버 호출
# =====================================================================
def ask(base: str, payload: dict, timeout: float = 600.0) -> dict:
    """
    /api/ask 는 NDJSON 스트림입니다. 한 줄씩 오고 종류가 섞여 있습니다.
        {"progress": {...}}   진행 상황
        {"ping": 1}           5초간 진전이 없을 때의 생존 신호
        {"result": {...}}     최종 결과
        {"error": "..."}      오류
        {"quota": {...}}      한도 초과
    마지막에 온 result/error/quota 를 돌려줍니다.
    """
    headers = {"Content-Type": "application/json"}
    if _AUTH_COOKIE:
        headers["Cookie"] = f"lawfinder_auth={_AUTH_COOKIE}"
    req = urllib.request.Request(
        base.rstrip("/") + "/api/ask",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    out = {"_progress": []}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "progress" in obj:
                    out["_progress"].append(obj["progress"])
                elif "result" in obj:
                    out.update(obj["result"])
                elif "error" in obj:
                    out["error"] = obj["error"]
                elif "quota" in obj:
                    out["quota"] = obj["quota"]
    except urllib.error.HTTPError as e:
        out["error"] = f"HTTP {e.code}: {e.read()[:300].decode('utf-8', 'replace')}"
    except Exception as e:                       # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# =====================================================================
# 되묻기 자동 응답
# =====================================================================
# 되묻기가 "결론"을 사용자에게 되묻는 꼴 — ai_client.CONCLUSION_Q 와 같은 규칙
# (이 파일은 표준 라이브러리만 쓰려고 import 하지 않고 복사해 둡니다)
CONCLUSION_Q = re.compile(r"언제까지|야\s*(합니|하나요|하는지|할까요|됩니|되나요|하는가)")


def pick_option(question: str, options: list, answers: dict, default: str) -> str:
    """
    되묻기 한 문항에 어떤 보기를 고를지 정합니다.

    answers 는 시나리오에 적어 둔 {"키워드": "고를 보기"} 입니다.
    질문에 그 키워드가 들어 있으면 해당 보기를 고릅니다.
    보기 목록에 정확히 일치하는 게 없으면 **부분 일치**로 한 번 더 찾고,
    그래도 없으면 그 값을 그대로 씁니다(자유 입력처럼 취급).

    아무 규칙도 안 걸리면 default 정책을 따릅니다.
      "모름"  — 모름 계열 보기를 고릅니다 (없으면 마지막)
      "first" — 첫 보기
    """
    # ★ 2026-10-29 (RAG 진단 해석30·34·37) — 자동 응답이 엉뚱한 값을 넣었습니다.
    #   · "지역" 키가 "재해취약지역에 포함됩니까?(예/아니오)" 에 걸려 '관리지역' 을 자유 입력으로 넣음(해석30)
    #   · "시설" 키가 "등록대상동물입니까?" 에 걸려 '반려견 운동·휴식시설' 을 넣음(해석37)
    #   → ① 질문에 든 키 중 **긴(구체적인) 키부터** 봅니다.
    #     ② 보기가 예/아니오 꼴인데 원하는 값이 보기와 안 맞으면: 그 값이 질문 문장에 들어 있으면 '예'
    #        ("3년 이상입니까?" ← "3년 이상"), 아니면 그 키는 건너뜁니다(자유 입력으로 넣지 않음).
    #   되돌리기: 아래를 예전 반복문(dict 순서, 무조건 자유 입력)으로.
    _ns = lambda t: re.sub(r"\s+", "", str(t or ""))
    yn = [o for o in options if re.match(r"^(예|아니|네)", str(o))]
    keys = sorted((k for k in (answers or {}) if k and k in question), key=len, reverse=True)
    for key in keys:
        want = answers[key]
        for o in options:
            if o == want:
                return o
        for o in options:
            if want in o or o in want:
                return o
        if yn:
            if _ns(want) and _ns(want) in _ns(question):
                return next((o for o in yn if re.match(r"^(예|네)", str(o))), yn[0])
            continue
        return want                          # 보기에 없으면 자유 입력값으로
    if default == "first" and options:
        return options[0]
    for o in options:
        if re.match(r"^(모름|모르|확인 안|미확인)", o):
            return o
    return options[-1] if options else "모름"


def run_case(base: str, case: dict, verbose: bool = True) -> dict:
    """시나리오 하나를 되묻기 끝까지 돌립니다."""
    q = case["question"]
    answers = case.get("answers", {})
    default = case.get("default", "모름")
    note = case.get("note", "")
    max_rounds = int(case.get("max_rounds", 8))

    answered = ""
    rounds = []
    clar_items = []          # 되묻기로 받은 질문·보기 원본 (채점용)
    t0 = time.time()
    laws = list(case.get("laws") or [])         # v1.33 — "참고할 법령" 지정 시나리오
    laws_mode = case.get("laws_mode", "only")   # "only" 이 법령에서만 / "prefer" 우선 참고
    prev = {"prev_question": case.get("prev_question", ""),          # v1.34 — 앞 질문에 이어서 묻기
            "prev_laws": list(case.get("prev_laws") or []),
            "prev_conclusion": case.get("prev_conclusion", "")}
    data = ask(base, {"question": q, "target": case.get("target", "auto"),
                      "answered": "", "round": 0, "laws": laws, "laws_mode": laws_mode,
                      "use_cache": False, **prev})   # v1.41 — 평가는 항상 새로 검색(저장본으로 통과하지 않게)

    r = 0
    while data.get("clarify") and r < max_rounds:
        r += 1
        picked = []
        for a in data["clarify"]:
            clar_items.append({"question": a.get("question", ""), "options": a.get("options", [])})
            choice = pick_option(a["question"], a.get("options", []), answers, default)
            picked.append(f"{a['question']}: {choice}")
            if verbose:
                print(f"    [{r}차] {a['question']}  →  {choice}")
        rounds.append(picked)
        merged = [x for x in (answered, " / ".join(picked)) if x]
        if note and r == 1:
            merged.append(f"추가 설명: {note}")
        answered = " / ".join(merged)
        data = ask(base, {"question": q, "target": case.get("target", "auto"),
                          "answered": answered, "round": r, "laws": laws, "laws_mode": laws_mode,
                          "use_cache": False, **prev})

    data["_elapsed"] = round(time.time() - t0, 1)
    data["_rounds"] = rounds
    data["_answered"] = answered
    data["_clarify_items"] = clar_items
    return data


# =====================================================================
# 채점
# =====================================================================
# ★ 2026-10-29 결론 중심 채점용
_CONCL_RE = re.compile(r"【결론】(.*?)(?=【|\Z)", re.S)
_COND_RE = re.compile(r"(경우|다면|라면|이면|으면|하면|되면|면\s*,|때에는|때는)")


def _conclusion_block(answer: str) -> str:
    """【결론】 블록(없으면 앞 400자)."""
    m = _CONCL_RE.search(answer or "")
    return (m.group(1) if m else (answer or "")[:400]).strip()


def grade(case: dict, data: dict) -> dict:
    """
    기대값과 대조합니다. 항목별로 통과/실패와 이유를 남깁니다.

    시나리오에 쓸 수 있는 기대 항목
      expect_cites      : 답변에 반드시 있어야 하는 인용 (부분 문자열 목록)
      forbid_cites      : 있으면 안 되는 인용
      expect_text       : 답변 본문에 있어야 하는 말
      expect_any        : [[대안1, 대안2, ...], ...] — 묶음마다 대안 중 하나만 있으면 통과
                          (공백 무시. "2028년 3월 15일" / "2028-03-15" 처럼 표기가 갈리는 것용)
      forbid_text       : 있으면 안 되는 말
      expect_laws       : 수집된 법령 이름에 있어야 하는 것
      forbid_laws       : 있으면 안 되는 법령 (되묻기로 제외됐어야 하는 것 등)
      max_rounds_used   : 되묻기가 이 횟수를 넘으면 실패
      expect_no_warning : true 면 별표 경고가 뜨면 실패
      expect_warning    : true 면 경고가 없으면 실패
      all_cites_ok      : true 면 인용 검증이 전부 통과해야 함
      expect_primary_law: 수집된 법령 중 **첫 번째**(주 법령)에 이 말이 있어야 함
                          (2026-09-29 — 맞는 법이 2순위로 밀려도 expect_laws 는 통과해서
                           방사성폐기물·가덕도신공항 법으로 답한 것을 못 잡았습니다)
      no_law_in_clarify : true 면 되묻기 질문·보기에 법령 이름이 있으면 실패
                          (2026-09-29 — "토양환경보전법 / 광산피해…법률 / 모름" 사고)
      expect_intent     : 서버가 판정한 질문 유형(intent.kind)이 이것이어야 함 (v1.31)
      no_clarify        : true 면 되묻기가 한 번도 없어야 함 (조문 조회·정의·일반 기준 질문)
      expect_context    : 답변 컨텍스트에 **실제로 들어가야 하는** 조문 ["법령명 제N조", "법령명 [별표 N]"]
                          — 검색·선별 단계 Recall. 답변이 틀렸을 때 원인이 검색인지 판단인지 가름
      expect_conclusion : [[대안…], …] — 【결론】 블록에 묶음마다 대안 하나 (2026-10-29 결론 중심 채점)
      forbid_conclusion : 【결론】 블록에 있으면 안 되는 말
      no_conclusion_in_clarify : true 면 되묻기가 결론(의무·기한)을 사용자에게 물으면 실패
                          (2026-09-29 — "다음 정기 누출검사를 언제까지 받아야 합니까?" 사고)
    """
    checks = []

    def ck(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    if data.get("error"):
        ck("서버 오류 없음", False, data["error"])
        return {"checks": checks, "score": 0, "total": 1}
    if data.get("quota"):
        ck("한도 초과 아님", False, str(data["quota"])[:200])
        return {"checks": checks, "score": 0, "total": 1}

    answer = data.get("answer", "") or ""
    # v1.39 (C-20261017-5) — 화면용 강조 표시(**굵게**, __밑줄__, !!강조!!)를 빼고 비교합니다.
    #   "**85제곱미터 기준**이 아닌" 이 금지어 "85제곱미터 기준이 아닌" 에 안 걸려 통과하던 문제.
    answer = re.sub(r"\*\*|__|!!", "", answer)
    laws = [l.get("name", "") for l in (data.get("laws") or [])]
    cites = data.get("citations") or []
    cite_str = " ".join(f"{c.get('law','')} {c.get('label','')}" for c in cites)
    warnings = data.get("warnings") or []

    for want in case.get("expect_cites", []):
        ck(f"인용 있음: {want}", want.replace(" ", "") in cite_str.replace(" ", ""),
           cite_str[:200])
    for bad in case.get("forbid_cites", []):
        ck(f"인용 없어야: {bad}", bad.replace(" ", "") not in cite_str.replace(" ", ""),
           cite_str[:200])
    for want in case.get("expect_text", []):
        ck(f"본문 포함: {want}", want in answer)
    flat = re.sub(r"\s+", "", answer)
    for group in case.get("expect_any", []):
        alts = group if isinstance(group, list) else [group]
        ck(f"본문 포함(택1): {' | '.join(alts)}",
           any(re.sub(r"\s+", "", a) in flat for a in alts))
    # ★ 2026-10-29 결론 중심 채점 — 판정은 【결론】 블록에서 봅니다.
    #   expect_conclusion : [[대안…], …] 묶음마다 대안 하나가 【결론】에 있어야 함(공백 무시)
    #   forbid_conclusion : 【결론】에 있으면 안 되는 말
    #   그리고 expect_conclusion 이 모두 맞은 경우, forbid_text 가 **조건 문장**("~경우에는/~이면/~다면 …")에만
    #   나오면 실패로 보지 않습니다 — '모름' 조건으로 경우를 나눈 답(해석30: "지구단위계획구역이면 허가")을
    #   틀린 답으로 세던 문제. 판정이 결론과 다르면 expect_conclusion 에서 걸립니다.
    concl = _conclusion_block(answer)
    flat_c = re.sub(r"\s+", "", concl)
    ec_ok = None
    if case.get("expect_conclusion"):
        ec_ok = True
        for group in case["expect_conclusion"]:
            alts = group if isinstance(group, list) else [group]
            ok = any(re.sub(r"\s+", "", a) in flat_c for a in alts)
            ec_ok = ec_ok and ok
            ck(f"결론 포함(택1): {' | '.join(alts)}", ok, "" if ok else concl[:160])
    for bad in case.get("forbid_conclusion", []):
        ck(f"결론에 없어야: {bad}", bad not in concl, concl[:160] if bad in concl else "")
    for bad in case.get("forbid_text", []):
        if bad not in answer:
            ck(f"본문 없어야: {bad}", True)
            continue
        sents = [x for x in re.split(r"(?<=[.。!?])\s+|\n", answer) if bad in x]
        cond_only = bool(ec_ok) and bool(sents) and all(_COND_RE.search(x.split(bad)[0]) for x in sents)
        ck(f"본문 없어야: {bad}", cond_only,
           "조건 문장에만 있음 — 결론 판정이 맞아 통과" if cond_only else (sents[0][:160] if sents else ""))
    for want in case.get("expect_laws", []):
        ck(f"법령 수집: {want}", any(want in l for l in laws), ", ".join(laws))
    if case.get("expect_primary_law"):
        want = case["expect_primary_law"]
        first = laws[0] if laws else ""
        ck(f"주 법령: {want}", bool(first) and want in first, first or "(수집된 법령 없음)")
    for bad in case.get("forbid_laws", []):
        ck(f"법령 제외: {bad}", not any(bad in l for l in laws), ", ".join(laws))

    if "max_rounds_used" in case:
        used = len(data.get("_rounds") or [])
        ck(f"되묻기 {case['max_rounds_used']}회 이하", used <= case["max_rounds_used"],
           f"실제 {used}회")
    if case.get("expect_no_warning"):
        ck("별표 경고 없음", not warnings, " / ".join(warnings)[:200])
    if case.get("expect_warning"):
        ck("별표 경고 있음", bool(warnings))
    if case.get("all_cites_ok"):
        bad = [c for c in cites if not c.get("ok")]
        ck("인용 검증 전부 통과", not bad,
           ", ".join(f"{c.get('law','')} {c.get('label','')}" for c in bad)[:200])

    if case.get("no_law_in_clarify"):
        def _law_like(t: str) -> bool:
            t = re.sub(r"[\s?？.]+$", "", str(t or ""))
            if re.search(r"(방법|용법|기법|공법|수법|요법)$", t):
                return False
            return bool(re.search(r"(법|법률|시행령|시행규칙)$", t)) and len(t) >= 4
        # v1.33 — 서버가 일부러 묻는 "어느 법의 규정을 찾으시나요?" 는 법령명을 보기로 쓰는 것이 정상입니다.
        bad = [f"{it['question']} [{' / '.join(it.get('options') or [])}]"
               for it in (data.get("_clarify_items") or [])
               if not str(it.get("question", "")).startswith("어느 법의 규정")
               and (_law_like(it.get("question")) or any(_law_like(o) for o in it.get("options") or []))]
        ck("되묻기에 법령 이름 없음", not bad, " | ".join(bad)[:200])

    if case.get("no_provision_in_clarify"):
        # v1.34 — "별표 3 기준 이상/미만" 처럼 사용자가 알 수 없는 조문·별표를 보기로 내면 실패
        bad = [f"{it['question']} [{' / '.join(it.get('options') or [])}]"
               for it in (data.get("_clarify_items") or [])
               if any(re.search(r"(별표|별지|제\s*\d+\s*조)", o) for o in (it.get("options") or []))]
        ck("되묻기 보기에 조문·별표 없음", not bad, " | ".join(bad)[:200])
    if case.get("clarify_has_unknown"):
        # v1.34 — 모든 되묻기 문항에 "모름" 보기가 있어야 함
        bad = [it["question"] for it in (data.get("_clarify_items") or [])
               if not any(str(o).startswith("모름") for o in (it.get("options") or []))]
        ck("되묻기마다 '모름' 보기 있음", not bad, " | ".join(bad)[:200])

    if case.get("forbid_clarify"):
        # v1.35 — 질문에 이미 있는 사실(예: "기름" → 석유류)을 되묻기로 다시 물으면 실패
        bad = [f"{it['question']} [{' / '.join(it.get('options') or [])}]"
               for it in (data.get("_clarify_items") or [])
               for w in case["forbid_clarify"]
               if re.search(w, str(it.get("question", "")) + " " + " ".join(it.get("options") or []))]
        ck(f"되묻기에 없어야: {' | '.join(case['forbid_clarify'])}", not bad, " | ".join(bad)[:200])

    if case.get("no_conclusion_in_clarify"):
        bad = [it.get("question", "") for it in (data.get("_clarify_items") or [])
               if CONCLUSION_Q.search(it.get("question", "") or "")]
        ck("되묻기가 결론을 묻지 않음", not bad, " | ".join(bad)[:200])

    # ── v1.31: 질문 유형 · 되묻기 없음 · 컨텍스트(검색 Recall) ──────────
    if case.get("expect_intent"):
        got = ((data.get("intent") or {}).get("kind")) or "(없음)"
        ck(f"질문 유형: {case['expect_intent']}", got == case["expect_intent"], f"실제 {got}")
    if case.get("no_clarify"):
        used = len(data.get("_rounds") or [])
        ck("되묻기 없음", used == 0, f"실제 {used}회")
    if case.get("expect_context"):
        keys = ((data.get("debug") or {}).get("context_keys")) or []
        dropped = ((data.get("debug") or {}).get("dropped_keys")) or []
        norm = lambda x: re.sub(r"\s+", "", str(x))
        for want in case["expect_context"]:
            w = norm(want)
            if "별표" in want or "별지" in want:
                hit = any(w in norm(k) for k in keys)
                was_dropped = any(w in norm(k) for k in dropped)
            else:
                hit = any(w == norm(k) for k in keys)
                was_dropped = any(w == norm(k) for k in dropped)
            ck(f"컨텍스트 포함: {want}", hit,
               "토큰 예산으로 빠짐" if was_dropped else f"컨텍스트 {len(keys)}개 중 없음")

    if not checks:                               # 기대값을 안 적었으면 최소 확인
        ck("답변이 비어 있지 않음", len(answer.strip()) > 20)

    return {"checks": checks,
            "score": sum(1 for c in checks if c["ok"]),
            "total": len(checks)}


# =====================================================================
# 보고서
# =====================================================================
def write_report(results: list, base: str) -> str:
    os.makedirs(OUTDIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = os.path.join(OUTDIR, f"report_{stamp}.md")
    js_path = os.path.join(OUTDIR, f"report_{stamp}.json")

    total_ok = sum(r["grade"]["score"] for r in results)
    total_all = sum(r["grade"]["total"] for r in results)
    passed = sum(1 for r in results if r["grade"]["score"] == r["grade"]["total"])

    L = []
    L.append(f"# 자동 테스트 보고서 — {stamp}")
    L.append("")
    L.append(f"- 서버: {base}")
    L.append(f"- 시나리오: **{passed}/{len(results)} 통과**  "
             f"(세부 검사 {total_ok}/{total_all})")
    # ── v1.31: 층별 지표 (어느 단계에서 틀리는지) ──────────────────
    def _layer(prefix):
        ok = tot = 0
        for r in results:
            for c in r["grade"]["checks"]:
                if c["name"].startswith(prefix):
                    tot += 1
                    ok += 1 if c["ok"] else 0
        return f"{ok}/{tot}" if tot else "-"
    over = [r["name"] for r in results
            if r["case"].get("no_clarify") and (r["data"].get("_rounds") or [])]
    L.append(f"- 검색(컨텍스트 Recall): **{_layer('컨텍스트 포함')}**  ·  "
             f"질문 유형: {_layer('질문 유형')}  ·  "
             f"과잉 되묻기: {len(over)}건" + (f" ({', '.join(over)})" if over else "")
             + f"  ·  되묻기 품질(결론 질문 없음): {_layer('되묻기가 결론')}")
    L.append("")
    L.append("| 시나리오 | 결과 | 되묻기 | 소요 |")
    L.append("|---|---|---|---|")
    for r in results:
        g = r["grade"]
        mark = "✅" if g["score"] == g["total"] else "❌"
        L.append(f"| {r['name']} | {mark} {g['score']}/{g['total']} | "
                 f"{len(r['data'].get('_rounds') or [])}회 | "
                 f"{r['data'].get('_elapsed', '?')}초 |")
    L.append("")

    for r in results:
        d, g = r["data"], r["grade"]
        L.append("---")
        L.append("")
        L.append(f"## {r['name']}")
        L.append("")
        L.append(f"**질문** {r['case']['question']}")
        L.append("")
        _it = (d.get("intent") or {})
        _ck = ((d.get("debug") or {}).get("context_keys")) or []
        if _it or _ck:
            L.append(f"- 질문 유형: {_it.get('label', '?')} · 컨텍스트 조문 {len(_ck)}개: "
                     + ", ".join(_ck[:12]) + (" …" if len(_ck) > 12 else ""))
            L.append("")
        for i, rd in enumerate(d.get("_rounds") or [], 1):
            L.append(f"- {i}차 되묻기: " + " / ".join(rd))
        if d.get("_rounds"):
            L.append("")

        L.append("**검사 결과**")
        L.append("")
        for c in g["checks"]:
            mark = "✅" if c["ok"] else "❌"
            line = f"- {mark} {c['name']}"
            if not c["ok"] and c.get("detail"):
                line += f"  \n      → {c['detail']}"
            L.append(line)
        L.append("")

        if d.get("error"):
            L.append(f"**오류** `{d['error']}`")
            L.append("")
            continue

        steps = d.get("steps") or []
        if steps:
            L.append("<details><summary>처리 과정</summary>")
            L.append("")
            for s in steps:
                det = s.get("detail")
                if isinstance(det, list):
                    det = ", ".join(str(x) for x in det)
                L.append(f"- **{s.get('name','')}** — {str(det)[:300]}")
            L.append("")
            L.append("</details>")
            L.append("")

        if d.get("warnings"):
            L.append("**경고**")
            L.append("")
            for w in d["warnings"]:
                L.append(f"- {w}")
            L.append("")

        cites = d.get("citations") or []
        if cites:
            L.append("**인용 검증**")
            L.append("")
            for c in cites:
                mark = "✅" if c.get("ok") else "❌"
                L.append(f"- {mark} {c.get('law','')} {c.get('label','')}"
                         + (f" — {c.get('note','')}" if c.get("note") else ""))
            L.append("")

        L.append("**답변**")
        L.append("")
        L.append("```")
        L.append((d.get("answer") or "(없음)")[:3000])
        L.append("```")
        L.append("")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    with open(js_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    return md_path


# =====================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--only", default="", help="이름에 이 말이 들어간 시나리오만 (| 로 여러 개)")
    ap.add_argument("--cases", default=CASES)
    ap.add_argument("--raw-file", default="",
                    help="v1.35 — 한 줄에 'target|value|mode|k=v&k=v' 하나씩. /api/raw 결과를 _eval/raw_N.xml 로 저장(개발용)")
    ap.add_argument("--probe-file", default="",
                    help="(진단용) 한 줄에 질문 하나. 서버 /api/raw 로 법제처 지능형 검색 원본 XML 을 "
                         "_eval/probe_ai_N.xml 로 저장하고 끝냅니다.")
    # ★ v1.41b (2026-10-22) — 시나리오 동시 실행 개수. 기본 1 = 예전과 똑같이 하나씩.
    #   되돌리기: 이 줄과 아래 "★ v1.41b" 블록을 지우고 "원래 반복문" 주석을 풀면 됩니다.
    ap.add_argument("--jobs", type=int, default=1,
                    help="v1.41b — 시나리오 N개를 동시에 돌립니다(최대 6). llama 슬롯이 1개면 3 정도가 적당")
    # ★ v1.32 — `_eval\\ARGS` 파일이 있으면 그 내용을 인자로 한 번만 덧붙입니다(읽고 지움).
    #   감시 프로그램을 다시 켜지 않아도 "다음 실행은 eval_extra.json 으로" 를 지정할 수 있습니다.
    #   예) ARGS 내용: --cases eval_extra.json
    argv = sys.argv[1:]
    args_file = os.path.join(OUTDIR, "ARGS")
    if os.path.exists(args_file):
        try:
            with open(args_file, encoding="utf-8", errors="replace") as f:
                extra = shlex.split(f.read().strip(), posix=False)
            os.remove(args_file)
        except OSError:
            extra = []
        if extra:
            print(f"_eval/ARGS 적용: {' '.join(extra)}")
            argv = argv + extra
    args = ap.parse_args(argv)
    # 쉼표로 여러 파일을 이어 돌릴 수 있습니다: --cases eval_cases.json,eval_extra.json
    case_files = []
    for one in str(args.cases).split(","):
        one = one.strip().strip('"')
        if one and not os.path.isabs(one) and not os.path.exists(one):
            one = os.path.join(HERE, one)
        if one:
            case_files.append(one)

    global _AUTH_COOKIE
    _AUTH_COOKIE = login(args.base)
    if _AUTH_COOKIE:
        print("로그인 완료 (APP_PASSWORD)")
    elif _env_value("APP_PASSWORD"):
        print("[경고] 로그인 실패 — 이후 요청이 401 로 실패할 수 있습니다.")

    if args.raw_file:
        rf = args.raw_file if os.path.isabs(args.raw_file) else os.path.join(HERE, args.raw_file)
        with open(rf, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
        os.makedirs(OUTDIR, exist_ok=True)
        for i, ln in enumerate(lines, 1):
            parts = (ln.split("|") + ["", "", "", ""])[:4]
            q = {"target": parts[0], "value": parts[1], "mode": parts[2] or "search"}
            q.update(urllib.parse.parse_qsl(parts[3]))
            url = args.base.rstrip("/") + "/api/raw?" + urllib.parse.urlencode(q)
            headers = {"Cookie": f"lawfinder_auth={_AUTH_COOKIE}"} if _AUTH_COOKIE else {}
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=90) as r:
                    body = r.read().decode("utf-8", errors="replace")
            except Exception as e:                    # noqa: BLE001
                body = f"ERROR {type(e).__name__}: {e}"
            out = os.path.join(OUTDIR, f"raw_{i}.xml")
            with open(out, "w", encoding="utf-8") as f:
                f.write(f"<!-- {ln} -->\n" + body[:400000])
            print(f"[raw {i}] {ln[:60]} → {out} ({len(body)}자)")
        return 0

    if args.probe_file:
        pf = args.probe_file if os.path.isabs(args.probe_file) else os.path.join(HERE, args.probe_file)
        with open(pf, encoding="utf-8") as f:
            queries = [ln.strip() for ln in f if ln.strip()]
        os.makedirs(OUTDIR, exist_ok=True)
        for i, q in enumerate(queries, 1):
            url = (args.base.rstrip("/") + "/api/raw?" + urllib.parse.urlencode(
                {"target": "aiSearch", "value": q, "display": "20", "search": "0"}))
            headers = {"Cookie": f"lawfinder_auth={_AUTH_COOKIE}"} if _AUTH_COOKIE else {}
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                    body = r.read().decode("utf-8", errors="replace")
            except Exception as e:                    # noqa: BLE001
                body = f"ERROR {type(e).__name__}: {e}"
            out = os.path.join(OUTDIR, f"probe_ai_{i}.xml")
            with open(out, "w", encoding="utf-8") as f:
                f.write(f"<!-- {q} -->\n" + body)
            print(f"[probe {i}] {q[:40]} → {out} ({len(body)}자)")
        return 0

    cases = []
    for cf in case_files:
        if not os.path.exists(cf):
            print(f"시나리오 파일이 없습니다: {cf}")
            return 2
        with open(cf, encoding="utf-8") as f:
            cases += json.load(f)
    if args.only:
        keys = [k for k in args.only.split("|") if k]       # "주유소|누출검사" 처럼 여러 개
        cases = [c for c in cases if any(k in c.get("name", "") for k in keys)]
    if not cases:
        print("실행할 시나리오가 없습니다.")
        return 2

    # ── 원래 반복문(v1.41 까지) — 되돌릴 때 아래 ★ v1.41b 블록 대신 이 주석을 푸세요 ──
    # results = []
    # for i, case in enumerate(cases, 1):
    #     name = case.get("name") or case["question"][:20]
    #     print(f"[{i}/{len(cases)}] {name}")
    #     data = run_case(args.base, case)
    #     g = grade(case, data)
    #     mark = "OK " if g["score"] == g["total"] else "FAIL"
    #     print(f"    {mark}  {g['score']}/{g['total']}  ({data.get('_elapsed','?')}초)")
    #     for c in g["checks"]:
    #         if not c["ok"]:
    #             print(f"      ✗ {c['name']}"
    #                   + (f" — {c['detail'][:120]}" if c.get("detail") else ""))
    #     results.append({"name": name, "case": case, "data": data, "grade": g})

    # ★ v1.41b (2026-10-22) — --jobs N 이면 시나리오 N개를 동시에 돌립니다.
    #   한 질문이 법제처 API 를 기다리는 동안 다른 질문이 LLM 을 쓰게 해서 전체 시간을 줄입니다.
    #   (llama-server 슬롯이 1개면 LLM 호출은 서버에서 줄을 섭니다 — 너무 크면 LLM 시간 초과)
    #   · jobs=1 이면 출력·순서·결과가 예전과 똑같습니다.
    #   · 동시 실행 때는 되묻기 진행 줄을 찍지 않고(섞임 방지), 결과는 시나리오 **원래 순서대로** 찍습니다.
    #   · 동시 실행 때 각 시나리오 시간(초)에는 줄 선 시간이 들어가 이전 기록과 비교할 수 없습니다.
    jobs = max(1, min(int(args.jobs or 1), 6))

    def _one(case: dict):
        name = case.get("name") or case["question"][:20]
        data = run_case(args.base, case, verbose=(jobs == 1))
        return name, data, grade(case, data)

    def _show(name: str, data: dict, g: dict):
        mark = "OK " if g["score"] == g["total"] else "FAIL"
        print(f"    {mark}  {g['score']}/{g['total']}  ({data.get('_elapsed','?')}초)")
        for c in g["checks"]:
            if not c["ok"]:
                print(f"      ✗ {c['name']}"
                      + (f" — {c['detail'][:120]}" if c.get("detail") else ""))

    results = []
    t_all = time.time()
    if jobs == 1:
        for i, case in enumerate(cases, 1):
            print(f"[{i}/{len(cases)}] {case.get('name') or case['question'][:20]}")
            name, data, g = _one(case)
            _show(name, data, g)
            results.append({"name": name, "case": case, "data": data, "grade": g})
    else:
        from concurrent.futures import ThreadPoolExecutor
        print(f"동시 실행 {jobs}개 (시나리오 {len(cases)}개) — 시나리오별 시간은 줄 선 시간 포함")
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            futs = [ex.submit(_one, case) for case in cases]
            for i, (case, fu) in enumerate(zip(cases, futs), 1):
                name, data, g = fu.result()
                print(f"[{i}/{len(cases)}] {name}")
                _show(name, data, g)
                results.append({"name": name, "case": case, "data": data, "grade": g})
    print(f"전체 걸린 시간: {time.time() - t_all:.0f}초 (동시 실행 {jobs}개)")

    path = write_report(results, args.base)
    ok = sum(1 for r in results if r["grade"]["score"] == r["grade"]["total"])
    print(f"\n{ok}/{len(results)} 시나리오 통과")
    print(f"보고서: {path}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:                             # noqa: BLE001
        # ★ 2026-08-20 — 여기서 안 잡으면 파이썬 기본 종료코드가 1 이라,
        #   "일부 시나리오 실패"(return 1) 랑 "진짜 크래시" 를 구분할 수 없었습니다.
        #   watch_and_test.py 가 그 둘을 구분해서 크래시일 때만 stderr 를 보여주도록
        #   크래시는 3 으로 구분해서 종료합니다.
        import traceback
        traceback.print_exc()
        sys.exit(3)
