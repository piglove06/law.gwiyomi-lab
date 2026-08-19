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
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CASES = os.path.join(HERE, "eval_cases.json")
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
    req = urllib.request.Request(
        base.rstrip("/") + "/api/ask",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
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
    for key, want in (answers or {}).items():
        if key and key in question:
            for o in options:
                if o == want:
                    return o
            for o in options:
                if want in o or o in want:
                    return o
            return want                      # 보기에 없으면 자유 입력값으로
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
    t0 = time.time()
    data = ask(base, {"question": q, "target": case.get("target", "auto"),
                      "answered": "", "round": 0})

    r = 0
    while data.get("clarify") and r < max_rounds:
        r += 1
        picked = []
        for a in data["clarify"]:
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
                          "answered": answered, "round": r})

    data["_elapsed"] = round(time.time() - t0, 1)
    data["_rounds"] = rounds
    data["_answered"] = answered
    return data


# =====================================================================
# 채점
# =====================================================================
def grade(case: dict, data: dict) -> dict:
    """
    기대값과 대조합니다. 항목별로 통과/실패와 이유를 남깁니다.

    시나리오에 쓸 수 있는 기대 항목
      expect_cites      : 답변에 반드시 있어야 하는 인용 (부분 문자열 목록)
      forbid_cites      : 있으면 안 되는 인용
      expect_text       : 답변 본문에 있어야 하는 말
      forbid_text       : 있으면 안 되는 말
      expect_laws       : 수집된 법령 이름에 있어야 하는 것
      forbid_laws       : 있으면 안 되는 법령 (되묻기로 제외됐어야 하는 것 등)
      max_rounds_used   : 되묻기가 이 횟수를 넘으면 실패
      expect_no_warning : true 면 별표 경고가 뜨면 실패
      expect_warning    : true 면 경고가 없으면 실패
      all_cites_ok      : true 면 인용 검증이 전부 통과해야 함
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
    for bad in case.get("forbid_text", []):
        ck(f"본문 없어야: {bad}", bad not in answer)
    for want in case.get("expect_laws", []):
        ck(f"법령 수집: {want}", any(want in l for l in laws), ", ".join(laws))
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
    ap.add_argument("--only", default="", help="이름에 이 말이 들어간 시나리오만")
    ap.add_argument("--cases", default=CASES)
    args = ap.parse_args()

    if not os.path.exists(args.cases):
        print(f"시나리오 파일이 없습니다: {args.cases}")
        return 2
    with open(args.cases, encoding="utf-8") as f:
        cases = json.load(f)
    if args.only:
        cases = [c for c in cases if args.only in c.get("name", "")]
    if not cases:
        print("실행할 시나리오가 없습니다.")
        return 2

    results = []
    for i, case in enumerate(cases, 1):
        name = case.get("name") or case["question"][:20]
        print(f"[{i}/{len(cases)}] {name}")
        data = run_case(args.base, case)
        g = grade(case, data)
        mark = "OK " if g["score"] == g["total"] else "FAIL"
        print(f"    {mark}  {g['score']}/{g['total']}  ({data.get('_elapsed','?')}초)")
        for c in g["checks"]:
            if not c["ok"]:
                print(f"      ✗ {c['name']}"
                      + (f" — {c['detail'][:120]}" if c.get("detail") else ""))
        results.append({"name": name, "case": case, "data": data, "grade": g})

    path = write_report(results, args.base)
    ok = sum(1 for r in results if r["grade"]["score"] == r["grade"]["total"])
    print(f"\n{ok}/{len(results)} 시나리오 통과")
    print(f"보고서: {path}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
