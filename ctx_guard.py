# -*- coding: utf-8 -*-
"""
★ v1.42 (2026-10-22) — 조문 컨텍스트 보호 (해석35)

문제 (평가 해석35, 2026-10-22 01:22)
    질문: "…지방계약법 시행령 제25조제1항제8호사목 … 에 따라 수의계약으로 해도 되나요?"
    · 조문 선별은 지방계약법 시행령 제25조를 골랐는데, 토큰 예산을 맞추는 '조문 분량 조절' 에서
      주 법령(폐기물관리법)이 아닌 법의 조문부터 빠지면서 **질문에 직접 적힌 조문**이 서식(별지)보다 먼저 빠졌습니다.
    · 게다가 예산 8,523 에 6,162 토큰만 남을 만큼 많이 뺐습니다(큰 조문 몇 개를 글자 수 추정으로 한꺼번에 뺌).

이 파일이 하는 일 (main.py 는 부르기만 합니다)
    1) cited_in_question() — 질문에 "법령명(약칭 포함) [시행령|시행규칙] 제N조(의M)" 로 적힌 조문을 찾아
       main.py 의 '질문에 이름이 나온 조문 보호'(named_ids)에 더합니다 → 예산 조절에서 마지막까지 남습니다.
    2) refill() — 분량 조절로 뺀 조문 중 우선순위가 높은 것부터, 예산 안에 들어가면 다시 넣습니다.

끄기: .env CTX_GUARD=0 (둘 다), CTX_GUARD_CITED=0 / CTX_GUARD_REFILL=0 (하나씩).
되돌리기: main.py 의 '★ v1.42 ctx_guard' 블록 두 곳을 지우고 이 파일을 지우면 됩니다.
"""
from __future__ import annotations

import os
import re
import time

_ART = re.compile(r"제\s*(\d{1,4})\s*조(?:\s*의\s*(\d{1,3}))?")
_TAILS = ("시행규칙", "시행령")


def enabled(name: str) -> bool:
    if os.getenv("CTX_GUARD", "1") == "0":
        return False
    return os.getenv(f"CTX_GUARD_{name.upper()}", "1") != "0"


def _norm(s: str) -> str:
    return re.sub(r"[\s·ㆍ「」『』]+", "", str(s or ""))


_ABBR_STATE = {"fail_t": 0.0, "id": None, "pairs": [], "by_full": {}}


def _abbr_table(law_client) -> dict:
    """
    약칭표 {정규화 약칭 → 정식 이름}. 프로세스에서 처음 한 번만 법제처에서 받습니다(law_client 가 기억).
    받기에 실패하거나 빈 표면 10분 동안 다시 받지 않습니다(인용마다 법제처를 다시 부르지 않게 — v1.42 검토 ①).
    """
    data = law_client._ABBR_CACHE.get("data")
    if data:
        return data
    if time.time() - _ABBR_STATE["fail_t"] < 600:
        return {}
    try:
        data = law_client.get_abbreviations()
    except Exception:                                    # noqa: BLE001
        data = {}
    if not data:
        _ABBR_STATE["fail_t"] = time.time()
    return data or {}


def _abbr_prepared():
    """(정규화 약칭, 정식 이름) 목록과 {정규화 정식 이름 → [정규화 약칭]} — 표가 바뀔 때만 다시 만듦(검토 ②)."""
    try:
        import law_client
        table = _abbr_table(law_client)
    except Exception:                                    # noqa: BLE001
        table = {}
    if _ABBR_STATE["id"] != id(table):
        pairs, by_full = [], {}
        for ab, full in table.items():
            abn = _norm(ab)
            pairs.append((abn, str(full).strip()))
            by_full.setdefault(_norm(full), []).append(abn)
        _ABBR_STATE.update(id=id(table), pairs=pairs, by_full=by_full)
    return _ABBR_STATE["pairs"], _ABBR_STATE["by_full"]


def _abbr_by_full() -> dict:
    """{정규화된 정식 법령명 → [정규화된 약칭, …]}"""
    return _abbr_prepared()[1]


def _name_forms(law_name: str, abbr: dict) -> list:
    """법령 이름이 질문에 적힐 수 있는 꼴(정규화). 긴 것부터."""
    n = _norm(law_name)
    base, tail = n, ""
    for t in _TAILS:
        if n.endswith(t):
            base, tail = n[: -len(t)], t
            break
    forms = {n}
    for ab in abbr.get(base, []):
        forms.add(ab + tail)
    return sorted(forms, key=len, reverse=True)


def cited_in_question(question: str, flat: list, limit: int = 4) -> list:
    """
    질문에 '법령명 + 제N조' 로 직접 적힌 조문 (flat 안에서만 찾음, 최대 limit 개).
    flat: [(f, a)] — f["name"] 법령명, a["조문번호"]·a["조문가지번호"].
    "공유재산법 제21조제3항" · "지방계약법 시행령 제25조제1항제8호사목" · "「농지법」 제42조" 같은 꼴.
    별표·서식은 제외합니다(조문만).
    """
    q = str(question or "")
    if not q or not flat:
        return []
    abbr = _abbr_by_full()
    laws = {}
    for f, a in flat:
        laws.setdefault(f.get("name", ""), f)
    forms = {name: _name_forms(name, abbr) for name in laws if name}
    out, seen = [], set()
    for m in _ART.finditer(q):
        before = _norm(q[max(0, m.start() - 60): m.start()])
        before = re.sub(r"(의|상|에따른|에서)$", "", before)
        raw_before = re.sub(r"[」』\s]+$", "", q[max(0, m.start() - 60): m.start()])
        best, best_len = None, 0
        for name, fs in forms.items():
            for fm in fs:
                if len(fm) >= 3 and before.endswith(fm) and len(fm) > best_len:
                    # 낱말 경계: "녹색건축법 제5조" 를 건축법 제5조로 보지 않게(검토 ⑥)
                    pat = r"(?:^|[^가-힣])" + r"\s*".join(map(re.escape, fm)) + r"(?:의|상|에\s*따른|에서)?$"
                    if not re.search(pat, raw_before):
                        continue
                    best, best_len = name, len(fm)
        if not best:
            continue
        jo, gaji = m.group(1), (m.group(2) or "")
        for f, a in flat:
            if f.get("name") != best:
                continue
            if str(a.get("구분", "")) in ("별표", "별지", "서식") or \
                    str(a.get("조문제목", "")).startswith(("[별표", "[별지", "[서식")):
                continue
            if str(a.get("조문번호", "")).lstrip("0") != jo.lstrip("0"):
                continue
            g = str(a.get("조문가지번호", "") or "").lstrip("0")
            if g != gaji.lstrip("0"):
                continue
            k = (best, jo, gaji)
            if k in seen:
                continue
            seen.add(k)
            out.append((f, a))
            break
        if len(out) >= limit:
            break
    return out


def refill(keep: list, dropped: list, prio: dict, pos: dict, assemble, count, budget: int,
           max_tries: int = 8):
    """
    분량 조절로 뺀 조문(dropped) 중 우선순위가 높은 것(prio 작은 것)부터 하나씩 넣어 보고,
    예산(budget) 안이면 남깁니다. 순서는 원래 자리(pos) 대로 되돌립니다.
    반환: (keep, dropped, context, n_laws, n_tok, back) — back 은 되돌려 넣은 것들.
    """
    keep = list(keep)
    dropped = list(dropped)
    back = []
    cands = sorted(dropped, key=lambda u: (prio.get(id(u[1]), 999.0), pos.get(id(u[1]), 9999)))
    tries = 0
    for u in cands:
        if tries >= max_tries:
            break
        tries += 1
        trial = sorted(keep + [u], key=lambda x: pos.get(id(x[1]), 9999))
        ctx, n_laws = assemble(trial)
        t = count(ctx)
        if t <= budget:
            keep = trial
            dropped.remove(u)
            back.append(u)
    context, n_laws = assemble(keep)
    return keep, dropped, context, n_laws, count(context), back


def named_by_abbr(prefix: str, laws: list) -> str:
    """
    ★ v1.42 (해석35 인용 검증) — 인용 앞 글자가 **공식 약칭**(+시행령/시행규칙)으로 끝나면 정식 법령명을 돌려줍니다.
      "「지방계약법 시행령」 제25조" 가 약칭이라 정식 이름 대조에 안 걸려, '시행령' 꼬리만 보고
      직전 법령(폐기물관리법)의 시행령 제25조로 붙어 '확인'으로 찍혔습니다.
    수집한 법령에 있으면 그 이름, 없으면 정식 이름(→ 수집한 조문에 없음으로 표시됨). 약칭이 아니면 "".
    """
    if not enabled("abbr"):
        return ""
    p = _norm(prefix)
    if not p:
        return ""
    pairs, _ = _abbr_prepared()
    if not pairs:
        return ""
    raw = str(prefix or "").rstrip()
    raw = re.sub(r"[」』\]\s]+$", "", raw)
    best, best_len = "", 0
    for abn, full in pairs:
        if len(abn) < 3 or abn[-1] not in "법률령칙정":
            continue
        for tail in _TAILS + ("",):
            form = abn + tail
            if len(form) <= best_len or not p.endswith(form):
                continue
            # 원문(공백 유지)에서 약칭 앞이 낱말 경계인지 확인("…특별법" 안의 일부가 아니어야 함)
            pat = r"(?:^|[^가-힣])" + r"\s*".join(map(re.escape, abn)) + (r"\s*" + tail if tail else "") + r"$"
            if not re.search(pat, raw):
                continue
            best, best_len = (full + (" " + tail if tail else "")), len(form)
    if not best:
        return ""
    for law in laws or []:
        if _norm(law.get("name", "")) == _norm(best):
            return law["name"]
    return best
