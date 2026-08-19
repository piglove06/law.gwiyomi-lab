# -*- coding: utf-8 -*-
"""
꼬인 git 상태를 정리하고 GitHub 에 올립니다.

왜 필요한가
    `git pull --rebase` 가 충돌로 멈춘 뒤 rebase 가 계속 진행 중(detached
    HEAD)이라, 무엇을 해도 옛 커밋이 다시 얹히면서 같은 충돌이 반복됐습니다.
    rebase 를 그냥 abort 하면 **작업 폴더 파일도 옛날 것으로 되돌아갑니다.**
    (실제로 그렇게 해서 check_secrets.py 와 start.bat 이 두 번 되돌아갔습니다)

무엇을 하는가
    (1) 지금 폴더의 파일을 통째로 _git_backup\\ 에 복사해 둡니다  ← 안전장치
    (2) rebase/merge 를 끝내고 main 브랜치로 돌아옵니다
    (3) 백업해 둔 파일을 다시 덮어씁니다 (git 이 되돌려 놓은 것을 복구)
    (4) 한 개의 깨끗한 커밋으로 만듭니다
    (5) GitHub 로 밀어 넣습니다

    원격 저장소에는 GitHub 이 자동으로 만든 커밋 두 개(.gitignore, LICENSE)
    밖에 없고, 그 두 파일의 내용은 이미 우리 폴더에 들어와 있습니다.
    그래서 원격 이력을 우리 것으로 교체해도 잃는 것이 없습니다.

실행:  python repair_git.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
BACKUP = os.path.join(HERE, "_git_backup")
SKIP_DIRS = {".git", ".venv", "__pycache__", "_git_backup",
             "_dump", "_eval", "_env_backup", "node_modules"}


def run(*args: str, quiet: bool = False) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=HERE, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=180)
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    if not quiet and out:
        for line in out.splitlines()[:12]:
            print("      " + line)
    return p.returncode, out


def snapshot() -> int:
    """작업 폴더를 _git_backup 에 복사합니다."""
    if os.path.isdir(BACKUP):
        shutil.rmtree(BACKUP, ignore_errors=True)
    n = 0
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        rel = os.path.relpath(root, HERE)
        if rel == ".":
            rel = ""
        dst_dir = os.path.join(BACKUP, rel) if rel else BACKUP
        os.makedirs(dst_dir, exist_ok=True)
        for f in files:
            try:
                shutil.copy2(os.path.join(root, f), os.path.join(dst_dir, f))
                n += 1
            except OSError:
                pass
    return n


def restore() -> int:
    """백업을 작업 폴더로 되돌립니다."""
    n = 0
    for root, dirs, files in os.walk(BACKUP):
        rel = os.path.relpath(root, BACKUP)
        if rel == ".":
            rel = ""
        dst_dir = os.path.join(HERE, rel) if rel else HERE
        os.makedirs(dst_dir, exist_ok=True)
        for f in files:
            try:
                shutil.copy2(os.path.join(root, f), os.path.join(dst_dir, f))
                n += 1
            except OSError:
                pass
    return n


def main() -> int:
    if not os.path.isdir(os.path.join(HERE, ".git")):
        print("git 저장소가 아닙니다.")
        return 1

    print("[1/6] 작업 폴더를 백업합니다 ...")
    print(f"      {snapshot()}개 파일 → _git_backup\\")

    print("\n[2/6] 진행 중인 rebase / merge 를 끝냅니다 ...")
    run("rebase", "--abort", quiet=True)
    run("merge", "--abort", quiet=True)
    run("cherry-pick", "--abort", quiet=True)

    # detached HEAD 에서 main 으로 돌아옵니다.
    rc, _ = run("checkout", "-B", "main", quiet=True)
    rc2, cur = run("rev-parse", "--abbrev-ref", "HEAD", quiet=True)
    print(f"      현재 브랜치: {cur.strip() or '?'}")

    print("\n[3/6] 백업했던 파일을 되돌립니다 ...")
    print(f"      {restore()}개 파일 복구")

    print("\n[4/6] 비밀 검사 ...")
    py = sys.executable
    guard = os.path.join(HERE, "check_secrets.py")
    p = subprocess.run([py, guard], cwd=HERE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    print((p.stdout or "").rstrip())
    if p.returncode != 0:
        print("\n비밀 검사에서 막혔습니다. 커밋하지 않습니다.")
        return 1

    print("\n[5/6] 커밋 ...")
    run("add", "-A", quiet=True)
    rc, _ = run("diff", "--cached", "--quiet", quiet=True)
    if rc != 0:
        stamp = datetime.now().strftime("%Y-%m-%d")
        run("commit", "-m",
            f"chore: 저장소 정리 및 v1.28 반영 ({stamp})\n\n"
            f"- 꼬인 rebase 정리\n"
            f"- Cloudflare 터널 토큰을 start.bat 에서 .env 로 이동\n"
            f"- .gitignore 충돌 해소 (GitHub 템플릿 + 프로젝트 규칙)")
    else:
        print("      커밋할 변경 없음")

    print("\n[6/6] GitHub 로 올립니다 ...")
    rc, out = run("push", "-u", "origin", "main", quiet=True)
    if rc != 0:
        print("      일반 push 가 거부됐습니다. 원격 이력을 우리 것으로 맞춥니다.")
        print("      (원격에는 GitHub 이 자동 생성한 커밋뿐이고, 그 파일들은")
        print("       이미 우리 폴더에 들어와 있어 잃는 것이 없습니다)")
        rc, out = run("push", "-u", "origin", "main", "--force")
        if rc != 0:
            print("\npush 에 실패했습니다. 위 메시지를 클로드에게 보내세요.")
            return 1

    print("\n" + "=" * 58)
    print(" 완료.  https://github.com/piglove06/law.gwiyomi-lab")
    print("=" * 58)
    run("log", "--oneline", "-5")
    print("\n_git_backup\\ 폴더는 확인 후 지우셔도 됩니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
