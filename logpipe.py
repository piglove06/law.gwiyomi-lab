# -*- coding: utf-8 -*-
"""
다른 프로그램(llama-server, cloudflared)의 로그를 읽기 좋게 바꿔 찍습니다.

    사용 (start.bat 이 알아서 씁니다)
        llama-server ... 2>&1 | python -u logpipe.py llm
        cloudflared tunnel run 2>&1 | python -u logpipe.py tunnel

무엇을 하는가
    - 줄 앞에 서울 시간 [MM-DD HH:MM:SS] 을 붙입니다.
      · cloudflared 는 UTC 로 찍습니다(2026-09-28T16:30:32Z). 서울 시간으로 바꿉니다.
      · llama-server 는 켠 뒤 지난 시간(0.15.135.181)만 찍습니다. 실제 시각으로 바꿉니다.
    - 색상 코드를 지웁니다. cmd 창에서 "[34m" 같은 글자로 보이던 것입니다.
    - llama-server 의 반복 잡음 줄은 창에서 숨기고, 요청마다 입력/출력 한 줄로 요약합니다.
    - 원본은 한 줄도 빠짐없이 _runs/llm_server_YYYYMMDD.log · tunnel_YYYYMMDD.log 에 남깁니다.

표준 라이브러리만 씁니다. 여기서 오류가 나도 줄을 버리지 않고 그대로 찍습니다
(이 스크립트가 죽으면 파이프가 막혀 llama-server 까지 멈출 수 있기 때문입니다).
"""
from __future__ import annotations

import os
import queue
import re
import sys
import threading
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "_runs")

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
CF_TIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?Z\s*")
LLAMA_ELAPSED_RE = re.compile(r"^\d+\.\d{2}\.\d{3}\.\d{3}\s+")      # 분.초.밀리초.마이크로초

# llama-server 창에서 숨길 줄 (파일에는 남습니다)
LLM_NOISE = (
    "graphs reused", "get_availabl", "launch_slot_", "stop processing",
    "unused tensor", "prompt processing, n_tokens", "n_gen =", "total time =",
    "cancel task",
)
PROMPT_RE = re.compile(r"task (\d+) \| prompt eval time =\s*([\d.]+) ms /\s*(\d+) tokens.*?([\d.]+) tokens per second")
EVAL_RE = re.compile(r"task (\d+) \|\s+eval time =\s*([\d.]+) ms /\s*(\d+) tokens.*?([\d.]+) tokens per second")


def now() -> datetime:
    return datetime.now(KST)


def stamp(dt: datetime | None = None) -> str:
    return (dt or now()).strftime("%m-%d %H:%M:%S")


def save(kind: str, text: str) -> None:
    try:
        os.makedirs(RUNS, exist_ok=True)
        name = "llm_server" if kind == "llm" else kind
        with open(os.path.join(RUNS, f"{name}_{now():%Y%m%d}.log"), "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass


def _print(text: str) -> None:
    try:
        print(text, flush=True)
    except Exception:                                    # noqa: BLE001
        try:
            print(text.encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:                                # noqa: BLE001
            pass


# ★ v1.33 — 화면 출력은 별도 스레드로. 2026-10-08 새벽, 누군가 이 창을 클릭해 콘솔이 "빠른 편집"
#   선택 상태가 되자 print 가 멈췄고 → 이 스크립트가 파이프를 안 비워서 → llama-server 가 로그를
#   못 쓰고 멈췄습니다(요청이 300초씩 시간 초과). 이제 화면이 멈춰도 파이프 읽기·파일 저장은 계속하고,
#   밀린 화면 줄은 일정량 넘으면 버립니다(파일에는 전부 남음).
_OUT: "queue.Queue[str | None]" = queue.Queue(maxsize=2000)


def _printer() -> None:
    while True:
        t = _OUT.get()
        if t is None:
            return
        _print(t)


def show(text: str) -> None:
    try:
        _OUT.put_nowait(text)
    except queue.Full:
        pass


def _disable_quick_edit() -> None:
    """Windows 콘솔의 "빠른 편집 모드" 를 끕니다(창을 클릭해도 출력이 멈추지 않게)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        h = k32.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)   # 읽기·쓰기, 공유, OPEN_EXISTING
        if not h or h == ctypes.c_void_p(-1).value:
            return
        mode = wintypes.DWORD()
        if k32.GetConsoleMode(h, ctypes.byref(mode)):
            k32.SetConsoleMode(h, (mode.value & ~0x0040) | 0x0080)        # QUICK_EDIT 끔, EXTENDED_FLAGS
        k32.CloseHandle(h)
    except Exception:                                    # noqa: BLE001
        pass


_pending: dict = {}      # task → 입력 요약 (출력 줄이 오면 합쳐서 한 줄로)


def format_line(kind: str, line: str) -> str | None:
    """화면에 찍을 줄. None 이면 숨김."""
    line = ANSI_RE.sub("", line).rstrip()
    if not line.strip():
        return None

    if kind == "tunnel":
        m = CF_TIME_RE.match(line)
        if m:
            utc = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            return f"[{stamp(utc.astimezone(KST))}] {line[m.end():]}"
        return f"[{stamp()}] {line}"

    if kind == "llm":
        body = LLAMA_ELAPSED_RE.sub("", line)
        is_err = body.startswith("E ") or " error" in body.lower()
        m = PROMPT_RE.search(body)
        if m:
            task, ms, n, tps = m.groups()
            _pending[task] = f"입력 {int(n):,} tok · {float(ms) / 1000:.1f}s ({float(tps):.0f} tok/s)"
            return None
        m = EVAL_RE.search(body)
        if m:
            task, ms, n, tps = m.groups()
            head = _pending.pop(task, "입력 ?")
            return (f"[{stamp()}] 요청 #{task}  {head}  →  출력 {int(n):,} tok · "
                    f"{float(ms) / 1000:.1f}s ({float(tps):.0f} tok/s)")
        if not is_err and any(k in body for k in LLM_NOISE):
            return None
        return f"[{stamp()}] {body}"

    return f"[{stamp()}] {line}"


def main() -> int:
    kind = (sys.argv[1] if len(sys.argv) > 1 else "log").lower()
    _disable_quick_edit()
    th = threading.Thread(target=_printer, daemon=True)
    th.start()
    stream = sys.stdin.buffer
    for raw in iter(stream.readline, b""):
        text = raw.decode("utf-8", "replace").rstrip("\r\n")
        try:
            save(kind, f"[{stamp()}] {ANSI_RE.sub('', text)}")
            out = format_line(kind, text)
        except Exception:                                # noqa: BLE001
            out = text
        if out is not None:
            show(out)
    try:
        _OUT.put(None, timeout=2)
        th.join(timeout=2)
    except Exception:                                    # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
