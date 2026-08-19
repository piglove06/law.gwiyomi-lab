# -*- coding: utf-8 -*-
"""
법령 조회 도우미 — 파일 감시 자동 테스트

무엇을 하는가
    한 번 켜두면 계속 떠 있으면서, 아래 둘 중 하나가 생기면
    **알아서** eval_run.py 를 돌리고 보고서를 남깁니다.

      (1) 소스 파일이 바뀜        (*.py, static/*.html, eval_cases.json)
      (2) `_eval\\RUN` 파일이 생김  ← 사람이 안 만들어도 됩니다

    (2) 가 핵심입니다. 클로드가 파일을 고쳐 넣으면 (1) 로 자동으로 돌고,
    "지금 한 번 더 돌려봐" 가 필요하면 `_eval\\RUN` 이라는 빈 파일만
    만들어 두면 됩니다. 버튼을 누를 사람이 필요 없습니다.

    결과는 항상 같은 이름으로도 남깁니다.
        _eval\\latest.md      ← 최신 보고서 (덮어씀. 이것만 보면 됩니다)
        _eval\\latest.json
        _eval\\report_<시각>.md   ← 이력용

쓰는 법
    (1) 다른 창에서 start.bat 으로 서버를 켜 둡니다
    (2) watch.bat 을 실행합니다 (또는 python watch_and_test.py)
    (3) 창을 그냥 열어 둡니다. Ctrl+C 로 멈춥니다.

옵션
    --no-commit     테스트 전에 git 커밋을 하지 않습니다
    --interval 2    감시 주기(초)
    --base URL      서버 주소

표준 라이브러리만 씁니다.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(HERE, "_eval")
RUNFILE = os.path.join(OUTDIR, "RUN")
STATEFILE = os.path.join(OUTDIR, "_watch_state.json")

# ★ 2026-08-20 — main.py 에 비밀번호 로그인(APP_PASSWORD)이 생긴 뒤로
#   /api/version 이 전부 401 을 내서 서버가 계속 "응답 없음"으로 보였습니다.
#   로그인해서 받은 쿠키를 붙여야 합니다. main.py 의 로그인 쿠키는 secure=True 라
#   urllib 의 자동 쿠키 처리(http.cookiejar)가 로컬 http 접속에는 다시 안 붙여줍니다
#   (문서화된 제약). 그래서 자동 처리에 맡기지 않고, 로그인 응답의 Set-Cookie 값을
#   직접 읽어 매 요청에 수동으로 붙입니다. (eval_run.py 와 동일한 방식)
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

# 감시할 파일. 이것들이 바뀌면 테스트를 다시 돌립니다.
WATCH = [
    "main.py", "ai_client.py", "law_client.py", "pdf_maker.py",
    "eval_cases.json", os.path.join("static", "index.html"),
]


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def snapshot() -> dict:
    """감시 대상 파일의 수정 시각을 모읍니다."""
    out = {}
    for rel in WATCH:
        p = os.path.join(HERE, rel)
        try:
            out[rel] = os.path.getmtime(p)
        except OSError:
            out[rel] = 0.0
    return out


def server_ready(base: str, timeout: float = 2.0) -> bool:
    """서버가 응답하는지 봅니다. 오토리로드 중이면 잠깐 죽어 있습니다."""
    global _AUTH_COOKIE
    headers = {}
    if _AUTH_COOKIE:
        headers["Cookie"] = f"lawfinder_auth={_AUTH_COOKIE}"
    try:
        req = urllib.request.Request(base.rstrip("/") + "/api/version",
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        if e.code == 401:
            # 로그인이 안 됐거나(오토리로드로 서버가 막 켜짐) 쿠키가 만료됐을 수 있습니다.
            _AUTH_COOKIE = login(base)
        return False
    except Exception:                            # noqa: BLE001
        return False


def wait_for_server(base: str, limit: float = 90.0) -> bool:
    """오토리로드가 끝나 서버가 다시 뜰 때까지 기다립니다."""
    t0 = time.time()
    warned = False
    while time.time() - t0 < limit:
        if server_ready(base):
            return True
        if not warned:
            log("서버 응답을 기다리는 중… (오토리로드 중이거나 꺼져 있습니다)")
            warned = True
        time.sleep(2)
    return False


def changed_files() -> list[str]:
    """스테이지된 파일 목록. 커밋 메시지를 자동으로 짓는 데 씁니다."""
    try:
        p = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=HERE,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
        return [l.strip() for l in (p.stdout or "").splitlines() if l.strip()]
    except Exception:                            # noqa: BLE001
        return []


def auto_message(files: list[str]) -> str:
    """
    바뀐 파일을 보고 커밋 메시지를 짓습니다.

    사람이 나중에 이력을 훑을 때 "무엇이 바뀐 커밋인지" 한 줄로 보이게
    합니다. 접두사는 흔히 쓰는 규칙을 따릅니다(feat/fix/docs/test/chore).
    """
    if not files:
        return "chore: 변경 사항 저장"
    names = [os.path.basename(f) for f in files]
    kinds = set()
    for n in names:
        if n in ("main.py", "ai_client.py", "law_client.py", "pdf_maker.py"):
            kinds.add("src")
        elif n == "index.html":
            kinds.add("ui")
        elif n.startswith("test_") or n.startswith("eval_"):
            kinds.add("test")
        elif n in ("CHANGELOG.md", "README.md") or n.endswith(".md"):
            kinds.add("docs")
        else:
            kinds.add("chore")

    if kinds == {"docs"}:
        head = "docs: 문서 갱신"
    elif kinds == {"test"}:
        head = "test: 테스트 갱신"
    elif "src" in kinds or "ui" in kinds:
        head = "fix: 소스 수정"
    else:
        head = "chore: 변경 사항 저장"

    shown = ", ".join(sorted(set(names))[:5])
    if len(set(names)) > 5:
        shown += f" 외 {len(set(names)) - 5}개"
    return f"{head} ({shown})"


def git_commit_push(push: bool = True) -> None:
    """
    검사 → 커밋 → 푸시. git 이 없거나 저장소가 아니면 조용히 넘어갑니다.

    ★ 저장소가 **공개**이므로 커밋 전에 check_secrets.py 를 반드시 돌립니다.
      한 번 올라간 키는 지워도 커밋 이력에 남습니다.
    """
    if not os.path.isdir(os.path.join(HERE, ".git")) or not shutil.which("git"):
        return
    try:
        subprocess.run(["git", "add", "-A"], cwd=HERE,
                       capture_output=True, timeout=60)
        r = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=HERE,
                           capture_output=True, timeout=60)
        if r.returncode == 0:
            return                               # 바뀐 것 없음

        guard = os.path.join(HERE, "check_secrets.py")
        if os.path.exists(guard):
            g = subprocess.run([python_exe(), guard], cwd=HERE,
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120)
            if g.returncode != 0:
                log("!! 비밀 검사 실패 — 커밋하지 않습니다.")
                for line in (g.stdout or "").splitlines():
                    print("    " + line, flush=True)
                subprocess.run(["git", "reset"], cwd=HERE,
                               capture_output=True, timeout=60)
                return

        msg = auto_message(changed_files())
        subprocess.run(["git", "commit", "-m", msg], cwd=HERE,
                       capture_output=True, timeout=60)
        log(f"커밋: {msg}")

        if push:
            p = subprocess.run(["git", "push", "origin", "main"], cwd=HERE,
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=180)
            if p.returncode == 0:
                log("GitHub 에 push 완료")
            else:
                err = ((p.stderr or "") + (p.stdout or "")).strip().splitlines()
                log("push 실패(커밋은 로컬에 남아 있습니다): "
                    + (err[-1][:160] if err else "원인 불명"))
                log("  첫 push 라면 한 번만: git push -u origin main")
    except Exception as e:                       # noqa: BLE001
        log(f"커밋/푸시 건너뜀: {e}")


def python_exe() -> str:
    venv = os.path.join(HERE, ".venv", "Scripts", "python.exe")
    return venv if os.path.exists(venv) else sys.executable


def run_eval(base: str) -> tuple[int, str]:
    """eval_run.py 를 돌리고 (종료코드, 최신 보고서 경로) 를 돌려줍니다."""
    cmd = [python_exe(), os.path.join(HERE, "eval_run.py"), "--base", base]
    log("테스트 시작…")
    try:
        p = subprocess.run(cmd, cwd=HERE, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=3600)
    except subprocess.TimeoutExpired:
        log("테스트가 1시간을 넘겨 중단했습니다.")
        return 3, ""
    for line in (p.stdout or "").splitlines():
        print("    " + line, flush=True)
    if p.returncode not in (0, 1) and p.stderr:
        print("    " + (p.stderr or "")[:2000], flush=True)

    # 가장 최근 보고서를 latest 로 복사합니다. 늘 같은 경로를 보면 되도록.
    try:
        reps = sorted((f for f in os.listdir(OUTDIR)
                       if f.startswith("report_") and f.endswith(".md")),
                      reverse=True)
        if reps:
            src = os.path.join(OUTDIR, reps[0])
            shutil.copyfile(src, os.path.join(OUTDIR, "latest.md"))
            js = src[:-3] + ".json"
            if os.path.exists(js):
                shutil.copyfile(js, os.path.join(OUTDIR, "latest.json"))
            return p.returncode, src
    except Exception as e:                       # noqa: BLE001
        log(f"보고서 정리 실패: {e}")
    return p.returncode, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--no-commit", action="store_true")
    ap.add_argument("--no-push", action="store_true",
                    help="커밋만 하고 GitHub 에 올리지 않습니다")
    ap.add_argument("--settle", type=float, default=4.0,
                    help="파일이 바뀐 뒤 이만큼 조용해지면 실행합니다(초)")
    args = ap.parse_args()

    global _AUTH_COOKIE
    os.makedirs(OUTDIR, exist_ok=True)
    log("감시를 시작합니다. Ctrl+C 로 멈춥니다.")
    log(f"  대상 : {', '.join(WATCH)}")
    log(f"  서버 : {args.base}")
    log(f"  즉시 실행하려면 이 파일을 만드세요 → {RUNFILE}")
    if not args.no_commit:
        log("  변경분은 자동으로 커밋"
            + ("됩니다." if args.no_push else " + GitHub push 됩니다."))
    if _env_value("APP_PASSWORD"):
        _AUTH_COOKIE = login(args.base)
        log("로그인 완료 (APP_PASSWORD)" if _AUTH_COOKIE
            else "로그인 실패 — 서버가 뜨면 자동으로 재시도합니다.")

    prev = snapshot()
    pending_since = 0.0

    while True:
        try:
            time.sleep(args.interval)

            reason = ""
            if os.path.exists(RUNFILE):
                try:
                    os.remove(RUNFILE)
                except OSError:
                    pass
                reason = "RUN 파일 요청"
                pending_since = 0.0
            else:
                cur = snapshot()
                changed = [k for k in cur if cur[k] != prev.get(k)]
                if changed:
                    prev = cur
                    pending_since = time.time()
                    log(f"변경 감지: {', '.join(changed)} — {args.settle:.0f}초 뒤 실행")
                    continue
                if pending_since and time.time() - pending_since >= args.settle:
                    reason = "소스 변경"
                    pending_since = 0.0

            if not reason:
                continue

            log(f"── 실행 ({reason}) " + "─" * 30)
            if not wait_for_server(args.base):
                log("서버가 응답하지 않아 이번 실행은 건너뜁니다.")
                _write_note("서버가 응답하지 않았습니다. start.bat 으로 서버를 켜 주세요.")
                continue

            if not args.no_commit:
                git_commit_push(push=not args.no_push)

            rc, path = run_eval(args.base)
            prev = snapshot()                    # 커밋이 mtime 을 건드려도 되돌립니다
            log(f"완료 (종료코드 {rc}) → _eval\\latest.md")
            log("─" * 46)

        except KeyboardInterrupt:
            log("멈춥니다.")
            return 0
        except Exception as e:                   # noqa: BLE001
            log(f"감시 루프 오류(계속합니다): {type(e).__name__}: {e}")
            time.sleep(3)


def _write_note(msg: str) -> None:
    """테스트를 못 돌렸을 때도 latest.md 에 이유를 남깁니다."""
    try:
        os.makedirs(OUTDIR, exist_ok=True)
        with open(os.path.join(OUTDIR, "latest.md"), "w", encoding="utf-8") as f:
            f.write(f"# 자동 테스트 — 실행하지 못했습니다\n\n"
                    f"- 시각: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                    f"- 사유: {msg}\n")
    except Exception:                            # noqa: BLE001
        pass


if __name__ == "__main__":
    sys.exit(main())
