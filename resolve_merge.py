# -*- coding: utf-8 -*-
"""
멈춰 있는 머지를 정리합니다.

왜 필요한가
    git pull 이 충돌로 멈추면, 파일 내용을 고쳐도 git 은 여전히
    "해결 안 됨" 으로 봅니다. `git add` 로 **해결했다고 알려줘야** 합니다.
    (실사용에서 .gitignore 를 고쳐 놨는데도 계속 막혔습니다)

무엇을 하는가
    (1) git 에게 미해결 파일 목록을 물어봅니다
    (2) 각 파일에 충돌 마커가 남아 있는지 봅니다
        · 남아 있으면  → 그 파일을 알려주고 멈춥니다 (사람이 고쳐야 함)
        · 깨끗하면     → git add 로 해결 처리합니다
    (3) 남은 미해결이 없으면 0, 있으면 1 을 돌려줍니다

    ★ 마커 문자열을 소스에 직접 쓰지 않고 만들어 씁니다.
      안 그러면 이 파일 자신이 "충돌 있는 파일" 로 잡힙니다.
      (배치파일 버전에서 실제로 그 사고가 났습니다)
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# 소스에 마커를 그대로 적지 않기 위해 조립합니다.
MARKS = ("<" * 7, "=" * 7, ">" * 7)


def git(*args: str) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=HERE, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=90)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def has_markers(path: str) -> bool:
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith(MARKS[0]) or line.startswith(MARKS[2]):
                    return True
                if line.rstrip("\r\n") == MARKS[1]:
                    return True
    except OSError:
        return False
    return False


def main() -> int:
    _, out = git("diff", "--name-only", "--diff-filter=U")
    unresolved = [l.strip() for l in out.splitlines() if l.strip()]
    if not unresolved:
        print("미해결 파일 없음.")
        return 0

    print(f"미해결 파일 {len(unresolved)}개:")
    dirty, fixed = [], []
    for rel in unresolved:
        full = os.path.join(HERE, rel.replace("/", os.sep))
        if has_markers(full):
            dirty.append(rel)
            print(f"  [손봐야 함] {rel} — 충돌 표시가 아직 남아 있습니다")
        else:
            rc, msg = git("add", "--", rel)
            if rc == 0:
                fixed.append(rel)
                print(f"  [해결 처리] {rel}")
            else:
                dirty.append(rel)
                print(f"  [실패] {rel} — {msg.strip()[:120]}")

    if dirty:
        print()
        print("=" * 58)
        print(" 아직 해결 안 된 파일이 있습니다. 위 목록을 클로드에게 보내세요.")
        print("=" * 58)
        return 1

    print(f"\n{len(fixed)}개 모두 해결 처리했습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
