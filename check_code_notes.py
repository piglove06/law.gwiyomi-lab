# -*- coding: utf-8 -*-
"""CODE_NOTES.md 와 코드가 어긋났는지 확인 (표준 라이브러리만).

  python check_code_notes.py           문서의 '### 파일 :: 이름' 이 코드에 없으면 실패(종료 코드 1)
  python check_code_notes.py --missing  설명이 없는 함수·클래스 목록도 보여 줌(실패로 치지 않음)
"""
import ast, os, re, sys, warnings

warnings.filterwarnings("ignore", category=SyntaxWarning)

HERE = os.path.dirname(os.path.abspath(__file__))
NOTES = os.path.join(HERE, "CODE_NOTES.md")
HEAD = re.compile(r"^### ([\w./-]+\.py) :: ([\w.]+)\s*$", re.M)


def symbols(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    out = set()

    def walk(node, prefix):
        for n in ast.iter_child_nodes(node):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.add(prefix + n.name)
                walk(n, prefix + n.name + ".")
            elif not prefix and isinstance(n, (ast.Assign, ast.AnnAssign)):
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                    for x in ([t] + list(getattr(t, "elts", []))):
                        if isinstance(x, ast.Name):
                            out.add(x.id)
    walk(tree, "")
    return out


def main():
    text = open(NOTES, encoding="utf-8").read()
    noted, bad, cache = {}, [], {}
    for f, name in HEAD.findall(text):
        path = os.path.join(HERE, f)
        if not os.path.exists(path):
            bad.append(f"{f} :: {name} — 파일 없음"); continue
        syms = cache.setdefault(f, symbols(path))
        noted.setdefault(f, set()).add(name)
        if name not in syms:
            bad.append(f"{f} :: {name} — 코드에 없음(이름이 바뀌었거나 지워짐)")
    for b in bad:
        print("✗", b)
    if "--missing" in sys.argv:
        for f, names in noted.items():
            funcs = {s for s in cache[f] if not s[:1].isupper() or "." in s}
            for s in sorted(funcs - names):
                print("· 설명 없음:", f, "::", s)
    print(f"확인: 문서 항목 {sum(len(v) for v in noted.values())}개, 어긋남 {len(bad)}개")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
