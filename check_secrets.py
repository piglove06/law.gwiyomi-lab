# -*- coding: utf-8 -*-
"""
커밋 직전 비밀 검사기.

왜 필요한가
    저장소가 **공개(public)** 입니다. 한 번 올라간 키는 나중에 지워도
    커밋 이력에 남습니다. 그래서 커밋 전에 무조건 한 번 걸러냅니다.

무엇을 보는가
    (1) .env / .env_* / _env_backup 이 git 추적 대상에 들어갔는지
        단, `.env.example` 같은 **예시 파일은 올라가야 합니다.**
    (2) 커밋될 파일 안에 키처럼 생긴 문자열이 있는지
    (3) 법제처 OC 값이 소스에 하드코딩됐는지

끝나면
    문제 없음 → 종료코드 0
    문제 있음 → 종료코드 1 (커밋 스크립트가 여기서 멈춥니다)

단독 실행도 됩니다:  python check_secrets.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

FORBIDDEN = re.compile(r"(^|/)\.env($|[._-])|(^|/)_env_backup/")

ALLOWED_ENV = re.compile(
    r"(^|/)\.env\.(example|sample|template|dist|defaults)$", re.I)

PATTERNS = [
    ("Google API 키",        re.compile(r"\bAIza[A-Za-z0-9_\-]{30,}")),
    ("Google OAuth 토큰",    re.compile(r"\bAQ\.[A-Za-z0-9_\-]{20,}")),
    ("OpenAI 키",           re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("GitHub 토큰",         re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("AWS 액세스 키",        re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Cloudflare 터널 토큰", re.compile(r"--token\s+ey[A-Za-z0-9_\-\.]{40,}")),
    ("JWT/토큰 문자열",      re.compile(r"\bey[A-Za-z0-9_\-]{60,}")),
    ("일반 비밀 대입",        re.compile(
        r"(?i)\b(api[_-]?key|secret|passwd|password|token)\s*=\s*"
        r"['\"][A-Za-z0-9_\-]{16,}['\"]")),
]

SKIP_FILES = {"check_secrets.py", "fix_tunnel_token.py"}

PLACEHOLDER = re.compile(
    r"(여기에|넣으세요|발급받은|아무거나|랜덤|입력하세요|바꾸세요|"
    r"your[_-]?key|xxx+|<[^>]+>|dummy|sample|example|change[_-]?me|placeholder)",
    re.I)


def git(*args: str) -> str:
    try:
        p = subprocess.run(["git", *args], cwd=HERE, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=60)
        return p.stdout or ""
    except Exception:                            # noqa: BLE001
        return ""


def main() -> int:
    if not os.path.isdir(os.path.join(HERE, ".git")):
        print("git 저장소가 아닙니다. 검사를 건너뜁니다.")
        return 0

    problems: list[str] = []

    tracked = [l.strip() for l in git("ls-files").splitlines() if l.strip()]
    for path in tracked:
        if FORBIDDEN.search(path) and not ALLOWED_ENV.search(path):
            problems.append(
                f"[치명] '{path}' 가 git 에 추적되고 있습니다. "
                f"API 키가 공개 저장소에 올라갑니다.\n"
                f"        해결:  git rm --cached \"{path}\"")

    staged = [l.strip() for l in
              git("diff", "--cached", "--name-only").splitlines() if l.strip()]
    targets = staged or tracked

    for rel in targets:
        if os.path.basename(rel) in SKIP_FILES:
            continue
        full = os.path.join(HERE, rel.replace("/", os.sep))
        if not os.path.isfile(full):
            continue
        if os.path.getsize(full) > 3_000_000:
            continue
        try:
            with open(full, encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except OSError:
            continue
        lines = text.splitlines()
        for label, pat in PATTERNS:
            for m in pat.finditer(text):
                line_no = text[:m.start()].count("\n") + 1
                line = lines[line_no - 1] if line_no <= len(lines) else ""
                if PLACEHOLDER.search(line):
                    continue
                problems.append(
                    f"[치명] {rel}:{line_no} 에 {label} 로 보이는 값이 있습니다.\n"
                    f"        {m.group(0)[:12]}…(가림)")

    for rel in targets:
        if os.path.basename(rel) in SKIP_FILES or not rel.endswith(".py"):
            continue
        full = os.path.join(HERE, rel.replace("/", os.sep))
        if not os.path.isfile(full):
            continue
        try:
            with open(full, encoding="utf-8", errors="ignore") as f:
                for i, line in enumerate(f, 1):
                    m = re.search(r"OC\s*=\s*['\"]([a-z0-9]{4,})['\"]", line)
                    if m and not PLACEHOLDER.search(line):
                        problems.append(
                            f"[경고] {rel}:{i} 에 법제처 OC 값이 직접 적혀 있습니다.\n"
                            f"        .env 로 옮기세요.")
        except OSError:
            pass

    if problems:
        print("=" * 62)
        print(" 비밀 검사 - 문제를 찾았습니다. 커밋을 멈춥니다.")
        print("=" * 62)
        for p in problems:
            print(" " + p)
        print("=" * 62)
        return 1

    print("비밀 검사 통과 (.env 미추적, 키 패턴 없음)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
