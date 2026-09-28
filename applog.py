# -*- coding: utf-8 -*-
"""
서버 로그 (콘솔 + 파일).

왜 따로 두나
    예전 로그는 단계마다 print 가 흩어져 있어서, 여러 질문이 섞이면
    "어떤 질문의 어느 단계인지" 알 수 없었습니다. 되묻기는 요청이 매번
    새로 오기 때문에 한 질문이 여러 조각으로 흩어져 보였습니다.

무엇을 하는가
    - 시각은 항상 **서울 시간(KST)** 으로 찍습니다. PC 시간대 설정과 무관합니다.
    - 질문 하나를 ▶ START ~ ■ END 블록으로 묶습니다.
      되묻기 왕복은 같은 번호(#MMDD-NNN)로 블록 안에 이어서 보여줍니다.
    - LLM 호출마다 단계·입력/출력 토큰·걸린 시간을 한 줄로 남깁니다.
    - 파일로도 남깁니다 (_runs/ 는 .gitignore 대상 — 공개 저장소에 안 올라감)
        _runs/lawfinder_YYYYMMDD.log   콘솔과 같은 내용 (줄임 없이)
        _runs/llm_debug_YYYYMMDD.log   LLM_DEBUG=1 일 때 프롬프트·응답 원문
      LLM 원문은 콘솔에 찍지 않습니다. 콘솔이 읽을 수 없을 만큼 길어졌습니다.

표준 라이브러리만 씁니다.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9), "KST")      # 한국은 서머타임이 없어 고정 오프셋으로 충분

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "_runs")

DEBUG = os.getenv("LLM_DEBUG", "0") not in ("0", "", "false", "False")

CONSOLE_WIDTH = 150          # 콘솔 한 줄 최대 글자 수 (파일에는 줄이지 않고 남김)
SESSION_TTL = 3600           # 되묻기 응답을 이 시간(초) 동안 안 하면 블록을 닫음
RULE = "─" * 78

_lock = threading.Lock()
_tls = threading.local()
_sessions: dict = {}
_counter = {"day": "", "n": 0}


# ── 기본 출력 ────────────────────────────────────────────────
def now() -> datetime:
    return datetime.now(KST)


def ts(dt: datetime | None = None) -> str:
    return (dt or now()).strftime("%m-%d %H:%M:%S")


def _append(prefix: str, text: str) -> None:
    try:
        os.makedirs(RUNS, exist_ok=True)
        path = os.path.join(RUNS, f"{prefix}_{now():%Y%m%d}.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass


def emit(line: str, full: str | None = None) -> None:
    """콘솔에는 line(길면 줄임), 파일에는 full(없으면 line)."""
    short = line if len(line) <= CONSOLE_WIDTH else line[:CONSOLE_WIDTH - 1] + "…"
    with _lock:
        try:
            print(short, flush=True)
        except (UnicodeEncodeError, OSError):
            try:
                print(short.encode("ascii", "replace").decode("ascii"), flush=True)
            except Exception:                        # noqa: BLE001
                pass
        _append("lawfinder", full if full is not None else line)


def _fmt(detail) -> str:
    """단계 detail(dict/list/str) 을 한 줄 문자열로."""
    if detail is None:
        return ""
    if isinstance(detail, dict):
        parts = []
        for k, v in detail.items():
            if isinstance(v, (list, tuple)):
                v = ", ".join(str(x) for x in v) or "-"
            parts.append(f"{k}: {v}")
        return " | ".join(parts)
    if isinstance(detail, (list, tuple)):
        return ", ".join(str(x) for x in detail)
    return " ".join(str(detail).split())            # 줄바꿈·연속 공백 정리


def _one_line(s: str, n: int = 80) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


# ── 질문 블록 ────────────────────────────────────────────────
class Session:
    def __init__(self, rid: str, key, question: str):
        self.rid = rid
        self.key = key
        self.question = question
        self.t0 = time.time()
        self.last_seen = self.t0
        self.active = 0.0            # 서버가 실제로 일한 시간 (사용자 응답 대기 제외)
        self.rounds = 0              # 되묻기 횟수
        self.llm_calls = 0
        self.tok_in = 0
        self.tok_out = 0
        self.prev_answered = ""


def _new_rid() -> str:
    day = f"{now():%m%d}"
    if _counter["day"] != day:
        # 서버가 코드 수정으로 다시 뜨면 번호가 001 부터 다시 시작해 같은 번호가
        # 겹쳤습니다. 오늘 로그 파일에서 마지막 번호를 찾아 이어서 씁니다.
        last = 0
        try:
            import re
            path = os.path.join(RUNS, f"lawfinder_{now():%Y%m%d}.log")
            with open(path, encoding="utf-8", errors="ignore") as f:
                for m in re.finditer(rf"#{day}-(\d{{3,}})", f.read()):
                    last = max(last, int(m.group(1)))
        except OSError:
            pass
        _counter["day"], _counter["n"] = day, last
    _counter["n"] += 1
    return f"{day}-{_counter['n']:03d}"


def _expire_old() -> None:
    cut = time.time() - SESSION_TTL
    for k in [k for k, s in _sessions.items() if s.last_seen < cut]:
        s = _sessions.pop(k)
        emit(f"[{ts()}] ■ END   #{s.rid}  되묻기 응답 없이 만료 · {_one_line(s.question, 50)}")
        emit(RULE)


def begin(key, question: str, round_no: int = 0, answered: str = "", note: str = "") -> Session:
    """요청 하나가 들어올 때. round 0 이면 새 블록, 그 외에는 이어서."""
    _expire_old()
    sess = _sessions.get(key) if round_no > 0 else None
    if sess is None:
        with _lock:
            sess = Session(_new_rid(), key, question)
        _sessions[key] = sess
        emit(RULE)
        emit(f"[{ts()}] ▶ START #{sess.rid}  {_one_line(question, 110)}",
             f"[{ts()}] ▶ START #{sess.rid}  {question}")
        if round_no > 0:
            emit(f"[{ts()}]  │ (서버 재시작 등으로 이전 기록이 없어 {round_no}차 되묻기부터 이어서 기록)")
    else:
        # 이번 라운드에서 새로 고른 답만 보여줍니다 (answered 는 누적 문자열).
        new = answered[len(sess.prev_answered):] if answered.startswith(sess.prev_answered) \
            else answered
        new = new.strip(" /")
        emit(f"[{ts()}]  │ ↩ 되묻기 {round_no}차 응답  {_one_line(new, 110) or '(없음)'}",
             f"[{ts()}]  │ ↩ 되묻기 {round_no}차 응답  {new}")
    if note.strip():
        emit(f"[{ts()}]  │   추가 설명: {_one_line(note, 100)}")
    sess.prev_answered = answered
    sess.last_seen = time.time()
    _tls.sess = sess
    _tls.t_req = time.time()
    return sess


def current() -> Session | None:
    return getattr(_tls, "sess", None)


def step(name: str, detail=None) -> None:
    """처리 단계 한 줄. main.py 의 steps.append 가 자동으로 부릅니다."""
    d = _fmt(detail)
    bar = "  │ " if current() else "  "
    line = f"[{ts()}]{bar}{name}" + (f" — {d}" if d else "")
    emit(line)


def llm(stage: str, prompt_tokens, output_tokens, secs: float,
        finish: str = "", tps=None, note: str = "", sent: bool = True) -> None:
    """LLM 호출 1회. sent=False 면 보내지 않은 것(합계에 안 넣음)."""
    s = current()
    if s and sent:
        s.llm_calls += 1
        s.tok_in += int(prompt_tokens or 0)
        s.tok_out += int(output_tokens or 0)
    pt = f"{int(prompt_tokens):,}" if prompt_tokens is not None else "?"
    ct = f"{int(output_tokens):,}" if output_tokens is not None else "?"
    speed = f" · {tps:.0f} tok/s" if tps else ""
    warn = ""
    if finish == "length":
        warn = "  ⚠ 출력 상한에 걸려 잘림"
    bar = "  │   " if s else "  "
    emit(f"[{ts()}]{bar}└ LLM {stage:<7} 입력 {pt} → 출력 {ct} tok · {secs:.1f}s{speed}{warn}"
         + (f" · {note}" if note else ""))


def warn(msg: str) -> None:
    bar = "  │ " if current() else "  "
    emit(f"[{ts()}]{bar}⚠ {_one_line(msg, 200)}", f"[{ts()}]{bar}⚠ {msg}")


def pause(sess: Session, asks: list, round_no) -> None:
    """되묻기를 화면에 보냈을 때. 블록은 닫지 않습니다."""
    sess.active += time.time() - getattr(_tls, "t_req", time.time())
    sess.rounds = int(round_no or sess.rounds + 1)
    sess.last_seen = time.time()
    emit(f"[{ts()}]  │ ? 되묻기 {sess.rounds}차 — 질문 {len(asks)}개, 사용자 응답 대기")
    for a in asks:
        opts = " / ".join(str(o) for o in (a.get("options") or []))
        emit(f"[{ts()}]  │     · {a.get('question', '')}  [{opts}]")
    _tls.sess = None


def end(sess: Session, error: str = "", summary: str = "") -> None:
    """최종 답변(또는 오류)을 돌려줄 때. 블록을 닫습니다."""
    sess.active += time.time() - getattr(_tls, "t_req", time.time())
    total = time.time() - sess.t0
    took = f"처리 {sess.active:.1f}s"
    if sess.rounds:
        took += f" (되묻기 {sess.rounds}회 포함 전체 {total:.0f}s)"
    llm_txt = (f" · LLM {sess.llm_calls}회 입력 {sess.tok_in:,} / 출력 {sess.tok_out:,} tok"
               if sess.llm_calls else "")
    if error:
        emit(f"[{ts()}] ■ END   #{sess.rid}  ✗ 실패 · {took}{llm_txt}")
        emit(f"[{ts()}]         원인: {_one_line(error, 130)}", f"[{ts()}]         원인: {error}")
    else:
        emit(f"[{ts()}] ■ END   #{sess.rid}  완료 · {took}{llm_txt}"
             + (f" · {_one_line(summary, 90)}" if summary else ""))
    emit(RULE)
    _sessions.pop(sess.key, None)
    _tls.sess = None


def clear() -> None:
    _tls.sess = None


# ── LLM 원문 (파일 전용) ─────────────────────────────────────
def debug(stage: str, text: str) -> None:
    """LLM_DEBUG=1 일 때만 파일에 남깁니다. 콘솔에는 찍지 않습니다."""
    if not DEBUG:
        return
    s = current()
    rid = f"#{s.rid} " if s else ""
    with _lock:
        _append("llm_debug", f"[{ts()}] {rid}[{stage}]\n{text}\n")
