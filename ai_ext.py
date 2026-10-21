# -*- coding: utf-8 -*-
"""
★ v1.42 ① (2026-10-22) — 법제처 지능형 검색(aiSearch) 더 쓰기   [기본 꺼짐]

지금까지(v1.31~)는 지능형 검색을 **질문 원문 한 번, search=0(법령 조문)** 만 불렀습니다.
2026-10-22 실호출(_eval/raw_1~3.xml)로 확인한 것:
    search=1 법령 별표·서식  → 별표 **제목·번호만** (본문 없음) — "어느 별표인지" 를 알려 줌
    search=2 행정규칙 조문   → 조문내용 전문까지 옴
    search=3 행정규칙 별표   → 제목만, 정확도 낮음 → 쓰지 않음
그래서 이 파일은 **이미 받아 둔 조문(flat) 안에서** 다음을 찾아 조문 선별 결과에 더합니다(새 법령을 받지는 않음).
    1) answer_hits — 되묻기 답(answered)이 생기면 '질문 + 답' 으로 search=0 을 다시 불러, 답에 맞는 조문을 앞에 둠
                     (처음 질문 한 줄로만 찾던 순위를 답으로 보정 — 해석23 처럼 엉뚱한 1위 보정)
    2) admrul_hits — search=2 로 행정규칙 조문(고시·훈령) 중 받아 둔 것 앞에 둠
    3) byl_hits    — search=1 로 별표 번호를 찾아, 받아 둔 별표(flat 의 '[별표 N] …')를 더함

켜기: _cache\\flags\\rag_ai_ext 파일 만들기 (또는 .env RAG_AI_EXT=1). 끄기: 파일 삭제.
되돌리기: main.py 의 '★ v1.42 ai_ext' 블록 삭제.
실패하면(법제처 오류 등) 아무것도 더하지 않습니다.
"""
from __future__ import annotations

import re
import time

import law_client
import rag_select

_MEMO: dict = {}
_MEMO_TTL = 600.0


def enabled() -> bool:
    return rag_select.enabled("ai_ext")


def _search(query: str, kind: str, display: int = 15) -> list:
    key = (query, kind, display)
    hit = _MEMO.get(key)
    if hit and time.time() - hit[0] < _MEMO_TTL:
        return hit[1]
    try:
        rows = law_client.ai_search(query, display=display, kind=kind)
    except Exception:                                    # noqa: BLE001
        rows = []
    if len(_MEMO) > 300:
        _MEMO.clear()
    _MEMO[key] = (time.time(), rows)
    return rows


def _n(x) -> str:
    return str(x or "").strip().lstrip("0")


def _nm(s) -> str:
    return re.sub(r"[\s·ㆍ]+", "", str(s or ""))


def _bp_key(a) -> tuple | None:
    mt = re.match(r"\[(별표|별지|서식)\s*(\d+)(?:의(\d+))?", str(a.get("조문제목", "")))
    return (mt.group(1), mt.group(2) + (f"의{mt.group(3)}" if mt.group(3) else "")) if mt else None


def _match_articles(rows: list, flat: list, name_field: str) -> list:
    """rows(조문 행) → flat 의 (f, a). 법령명은 공백·가운뎃점만 무시하고 정확히 같아야 함."""
    out, seen = [], set()
    for r in rows:
        nm = _nm(r.get(name_field))
        jo, gaji = _n(r.get("조문번호")), _n(r.get("조문가지번호"))
        if not nm or not jo:
            continue
        for f, a in flat:
            if _nm(f.get("name")) != nm or _bp_key(a):
                continue
            if _n(a.get("조문번호")) == jo and _n(a.get("조문가지번호")) == gaji:
                k = (f.get("name"), jo, gaji)
                if k not in seen:
                    seen.add(k)
                    out.append((f, a))
                break
    return out


def answer_hits(question: str, answered: str, flat: list, max_n: int = 4) -> list:
    """되묻기 답을 붙여 search=0 을 다시 부른 결과 중 받아 둔 조문(상위 max_n)."""
    if not answered or not flat:
        return []
    q = rag_select.query_text(question, answered)
    rows = _search(q[:400], "0", 15)
    return _match_articles(rows, flat, "법령명")[:max_n]


def admrul_hits(question: str, flat: list, max_n: int = 3) -> list:
    """search=2(행정규칙 조문) 결과 중 받아 둔 행정규칙 조문."""
    if not flat:
        return []
    rows = _search(question[:400], "2", 10)
    return _match_articles(rows, flat, "행정규칙명")[:max_n]


def byl_hits(question: str, flat: list, max_n: int = 3) -> list:
    """search=1(법령 별표·서식) 결과 중 '별표' 이고 받아 둔 것(flat 의 [별표 N])."""
    if not flat:
        return []
    rows = _search(question[:400], "1", 10)
    want = []
    for r in rows:
        if str(r.get("별표서식구분명", "")).strip() != "별표":
            continue
        try:
            no = str(int(r.get("별표서식번호", "0")))
            g = int(r.get("별표서식가지번호", "0") or 0)
        except (TypeError, ValueError):
            continue
        k = (_nm(r.get("법령명")), "별표", no + (f"의{g}" if g else ""))
        if k not in want:
            want.append(k)
    out = []
    for nm, kind, no in want:
        for f, a in flat:
            bk = _bp_key(a)
            if bk and _nm(f.get("name")) == nm and bk == (kind, no):
                out.append((f, a))
                break
        if len(out) >= max_n:
            break
    return out


def merge(picked: list, hits: list, akey, front: bool = True) -> tuple:
    """
    hits 를 picked 에 더합니다. front=True 면 맨 앞(이미 있으면 앞으로 옮김 — 우선순위 최상),
    False 면 없는 것만 뒤에 덧붙임(기존 선별을 밀어내지 않음). (새 picked, 새로 더한 것) 을 돌려줍니다.
    """
    if not hits:
        return picked, []
    have = {akey(f, a) for f, a in picked}
    keys, uniq = [], []
    for f, a in hits:
        k = akey(f, a)
        if k in keys:
            continue
        keys.append(k)
        uniq.append((f, a))
    added = [p for p in uniq if akey(*p) not in have]
    if not front:
        return picked + added, added
    ks = set(keys)
    return uniq + [p for p in picked if akey(*p) not in ks], added
