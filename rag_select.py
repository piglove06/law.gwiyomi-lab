"""
v1.41 RAG — 조문 '본문' 유사도로 조문 선별을 보강합니다.

왜 필요한가
  지금 조문 선별(ai_client.select_articles)은 LLM 이 조문 **제목 목록**만 보고 고릅니다.
  그래서 ① 핵심이 본문·별표에만 있는 조문, ② 목록이 길어 뒤가 잘린 조문(맨 끝 별표),
  ③ 질문 말과 법령 말이 다른 경우("동네 이장" ↔ "그 밖의 관계인")를 놓쳤습니다.

무엇을 하나
  - BM25(글자 2-gram) 로 질문(+되묻기 답)과 조문 본문(+머리말: 법령명·조문번호·제목)을 비교합니다.
    2026-10-21 오프라인 실험(전문 eval 34문항, 정답 조문이 후보 조문 중 몇 위인지):
      제목 BM25 Hit@20 0.76 → 본문+머리말 2-gram Hit@20 0.97, Hit@40 1.00
      (띄어쓰기 단위 0.68, 조사 제거 0.79, 3-gram 0.88 — 2-gram 이 가장 좋았음)
  - (선택) 임베딩 서버(EMBED_URL, llama-server --embedding)가 있으면 임베딩 유사도와 RRF 로 합칩니다.
  - LLM 선별 결과는 **그대로 두고**, 상위 후보 중 빠진 것만 **뒤에 덧붙입니다**.
    토큰 예산을 넘으면 뒤에서부터 빼므로 기존 선별 조문을 밀어내지 않습니다.

켜고 끄기 (기본 꺼짐)
  - 환경변수 RAG_SELECT / RAG_CLARIFY / RAG_HYDE 가 1 이면 켜짐, 0 이면 꺼짐(파일보다 우선).
  - rag_hyde: 가상 조문 질의 확장(LLM 1회 추가, rag_select 가 켜져 있을 때만 의미 있음).
  - 환경변수가 없으면 플래그 파일로 판단: _cache/flags/rag_select, rag_clarify, rag_hyde
    (파일만 만들거나 지우면 서버 재시작 없이 바로 켜고 끕니다.)
  - 이 모듈에서 예외가 나면 호출부가 기존 결과를 그대로 씁니다(아래 함수들은 예외를 삼킵니다).

원복
  main.py 의 '★ v1.41 RAG' 블록을 지우고 이 파일을 지우면 v1.40 과 같습니다.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
from array import array
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "_cache")
FLAG_DIR = os.path.join(CACHE_DIR, "flags")

def _num_env(name: str, default):
    try:
        return type(default)(os.getenv(name, str(default)))
    except ValueError:
        return default


TOPK = _num_env("RAG_TOPK", 12)          # 유사도 상위 몇 개를 후보로 볼지
MAX_ADD = _num_env("RAG_MAX_ADD", 6)    # 선별 결과에 최대 몇 개를 덧붙일지
DOC_CHARS = _num_env("RAG_DOC_CHARS", 6000)   # 긴 별표는 앞부분만 색인(속도)

# (선택) 임베딩 — 비어 있으면 BM25 만 씁니다.
EMBED_URL = os.getenv("EMBED_URL", "").strip()          # 예: http://127.0.0.1:8081/v1/embeddings
EMBED_MODEL = os.getenv("EMBED_MODEL", "embed").strip()
EMBED_QPREFIX = os.getenv("EMBED_QPREFIX", "")         # e5 계열이면 "query: "
EMBED_DPREFIX = os.getenv("EMBED_DPREFIX", "")         # e5 계열이면 "passage: "
EMBED_BUDGET = _num_env("EMBED_BUDGET", 20.0)  # 한 질문에서 임베딩 계산에 쓸 최대 초
RRF_K = 60


def enabled(name: str = "select") -> bool:
    env = os.getenv("RAG_" + name.upper())
    if env is not None and env.strip() != "":
        return env.strip() == "1"
    return os.path.exists(os.path.join(FLAG_DIR, "rag_" + name))


# ── 토큰화: 글자 2-gram ───────────────────────────────────────────
_WORD = re.compile(r"[가-힣a-zA-Z0-9]+")


def _tok(text: str) -> list:
    out = []
    for w in _WORD.findall((text or "").lower()):
        if len(w) <= 2:
            out.append(w)
        else:
            out.extend(w[i:i + 2] for i in range(len(w) - 1))
    return out


# 되묻기 답에서 검색에 도움이 안 되는 말(보기 문구)
_ANS_NOISE = re.compile(r"(모름|모르겠|해당\s*없음|기타|잘\s*모르)")


def query_text(question: str, answered: str = "") -> str:
    a = _ANS_NOISE.sub(" ", answered or "")
    return (question or "") + ("\n" + a if a.strip() else "")


# ── 질의 확장 ① 검색어 변환 결과(무료 — 이미 LLM 이 만든 법령 용어) ─────────────
#   1라운드 검색 단계의 '검색어 변환' 용어를 질문별로 기억해 두었다가 2라운드부터도 씁니다.
_TERMS_MEMO: dict = {}


def remember_terms(question: str, terms) -> list:
    q = (question or "").strip()
    if terms:
        _TERMS_MEMO[q] = [str(t) for t in terms][:12]
        if len(_TERMS_MEMO) > 500:
            _TERMS_MEMO.pop(next(iter(_TERMS_MEMO)))
    return _TERMS_MEMO.get(q, [])


# ── 질의 확장 ② 가상 조문(HyDE / KoBLEX PARSER 1단계) — 스위치 rag_hyde, LLM 1회(짧게) ──
#   질문은 생활어, 조문은 법령어라 글자가 잘 안 겹칩니다. LLM 에게 "답이 될 조문이라면 어떤 문장일지"를
#   법령 문체로 2~3문장 쓰게 하고 그 문장을 검색어에 더합니다. 조문 번호는 지어낼 수 있어 지웁니다.
#   쓰는 곳은 **검색어뿐** — 답변·되묻기에는 들어가지 않습니다(틀린 내용이 근거로 쓰이지 않게).
HYDE_PROMPT = """아래 질문에 답이 되는 한국 법령 조문이 있다면 어떤 문장일지, 법령 문체로 2~3문장만 써라.
- 법령명과 조문 번호(제○조)는 쓰지 마라.
- 대상·요건·행위·기준·처분·예외를 법령에서 쓰는 용어로 쓴다. 질문의 일상 표현은 법령 용어로 바꾼다(예: 가게 → 영업소).
- 설명·답변·인사말 없이 조문 문장만 쓴다.

[질문]
{q}
"""
_HYDE_MEMO: dict = {}
HYDE_MAXTOK = _num_env("RAG_HYDE_MAXTOK", 200)
_ART_NO = re.compile(r"제\s*\d+\s*조(?:\s*의\s*\d+)?(?:\s*제\s*\d+\s*항)?(?:\s*제\s*\d+\s*호)?")


def hyde(question: str, answered: str = "") -> str:
    key = ((question or "").strip(), (answered or "").strip())
    if key in _HYDE_MEMO:
        return _HYDE_MEMO[key]
    import ai_client                        # 지연 import (순환 방지)
    q = query_text(question, answered)
    raw = ai_client._call(HYDE_PROMPT.format(q=q), temperature=0, max_tokens=HYDE_MAXTOK, stage="hyde")
    txt = _ART_NO.sub(" ", str(raw or "")).strip()[:600]
    _HYDE_MEMO[key] = txt
    if len(_HYDE_MEMO) > 300:
        _HYDE_MEMO.pop(next(iter(_HYDE_MEMO)))
    return txt


def _label(a: dict) -> str:
    no, gaji = str(a.get("조문번호", "") or "").lstrip("0"), str(a.get("조문가지번호", "") or "").lstrip("0")
    t = a.get("조문제목", "") or ""
    if not no:
        return t
    return f"제{no}조" + (f"의{gaji}" if gaji else "") + (f"({t})" if t else "")


def doc_text(f: dict, a: dict) -> str:
    """머리말(법령명·조문번호·제목) + 본문 — Contextual Retrieval 식, LLM 없이 구조로."""
    body = (a.get("조문내용") or "")[:DOC_CHARS]
    return f"{f.get('name', '')} {_label(a)}\n{body}"


def _is_form(a: dict) -> bool:
    t = str(a.get("조문제목", ""))
    return a.get("구분") in ("별지", "서식") or t.startswith(("[별지", "[서식"))


# 토큰화 결과 메모(같은 조문을 라운드마다 다시 자르지 않게). 텍스트 해시 → Counter
_BAG_MEMO: dict = {}
_BAG_LOCK = threading.Lock()


def _bag(text: str) -> Counter:
    h = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()
    with _BAG_LOCK:
        b = _BAG_MEMO.get(h)
    if b is None:
        b = Counter(_tok(text))
        with _BAG_LOCK:
            if len(_BAG_MEMO) > 5000:
                _BAG_MEMO.clear()
            _BAG_MEMO[h] = b
    return b


def bm25_scores(query: str, docs: list, k1: float = 1.2, b: float = 0.75) -> list:
    bags = [_bag(d) for d in docs]
    n = len(bags)
    if not n:
        return []
    lens = [sum(x.values()) for x in bags]
    avg = (sum(lens) / n) or 1.0
    df = Counter()
    for x in bags:
        df.update(x.keys())
    qt = set(_tok(query))
    out = []
    for bag, dl in zip(bags, lens):
        s = 0.0
        for t in qt:
            tf = bag.get(t, 0)
            if tf:
                s += math.log(1 + (n - df[t] + .5) / (df[t] + .5)) * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avg))
        out.append(s)
    return out


# ── (선택) 임베딩: llama-server /v1/embeddings + sqlite 캐시 ───────────
_EMB_LOCK = threading.Lock()


def _emb_db():
    os.makedirs(CACHE_DIR, exist_ok=True)
    con = sqlite3.connect(os.path.join(CACHE_DIR, "emb.sqlite"), timeout=10)
    con.execute("CREATE TABLE IF NOT EXISTS emb (model TEXT, h TEXT, vec BLOB, PRIMARY KEY(model, h))")
    return con


def _post_embed(texts: list, timeout: float) -> list:
    import httpx  # requirements.txt 에 이미 있음
    r = httpx.post(EMBED_URL, json={"model": EMBED_MODEL, "input": texts}, timeout=timeout)
    r.raise_for_status()
    data = sorted(r.json()["data"], key=lambda x: x.get("index", 0))
    return [d["embedding"] for d in data]


def _unit(v: list) -> array:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return array("f", (x / n for x in v))


def embed_scores(query: str, docs: list) -> list | None:
    """임베딩 코사인 유사도. 서버가 없거나 시간 예산을 넘으면 None(=BM25 만 사용)."""
    if not EMBED_URL:
        return None
    t0 = time.time()
    hs = [hashlib.sha1((EMBED_DPREFIX + d).encode("utf-8", "ignore")).hexdigest() for d in docs]
    vecs: dict = {}
    with _EMB_LOCK:
        con = _emb_db()
        try:
            for i in range(0, len(hs), 500):
                part = hs[i:i + 500]
                q = "SELECT h, vec FROM emb WHERE model=? AND h IN (%s)" % ",".join("?" * len(part))
                for h, blob in con.execute(q, [EMBED_MODEL] + part):
                    a = array("f")
                    a.frombytes(blob)
                    vecs[h] = a
        finally:
            con.close()
    missing = [i for i, h in enumerate(hs) if h not in vecs]
    batch = 32
    new_rows = []

    def _flush():
        if new_rows:
            with _EMB_LOCK:
                con = _emb_db()
                try:
                    con.executemany("INSERT OR REPLACE INTO emb VALUES (?,?,?)", new_rows)
                    con.commit()
                finally:
                    con.close()
            new_rows.clear()

    for i in range(0, len(missing), batch):
        left = EMBED_BUDGET - (time.time() - t0)
        if left <= 1:
            _flush()                        # 이번 질문은 BM25 만. 계산해 둔 것은 저장해 다음에 씀
            return None
        idx = missing[i:i + batch]
        embs = _post_embed([EMBED_DPREFIX + docs[j] for j in idx], timeout=left)
        for j, e in zip(idx, embs):
            u = _unit(e)
            vecs[hs[j]] = u
            new_rows.append((EMBED_MODEL, hs[j], u.tobytes()))
        if len(new_rows) >= 256:
            _flush()
    _flush()
    qv = _unit(_post_embed([EMBED_QPREFIX + query], timeout=max(2.0, EMBED_BUDGET - (time.time() - t0)))[0])
    return [sum(x * y for x, y in zip(qv, vecs[h])) for h in hs]


def _rrf(*score_lists) -> list:
    n = len(score_lists[0])
    fused = [0.0] * n
    for sc in score_lists:
        order = sorted(range(n), key=lambda i: -sc[i])
        for rank, i in enumerate(order, 1):
            if sc[i] > 0:
                fused[i] += 1.0 / (RRF_K + rank)
    return fused


def rank(query: str, pairs: list) -> list:
    """pairs [(f, a)] → 점수 내림차순 인덱스 목록과 점수. 서식(별지)은 뺍니다."""
    idx = [i for i, (f, a) in enumerate(pairs) if not _is_form(a) and (a.get("조문내용") or "").strip()]
    docs = [doc_text(*pairs[i]) for i in idx]
    bm = bm25_scores(query, docs)
    sc = bm
    used = "BM25"
    try:
        em = embed_scores(query, docs)
    except Exception:                       # noqa: BLE001 — 임베딩은 보조. 실패하면 BM25 만
        em = None
    if em is not None:
        sc = _rrf(bm, em)
        used = "BM25+임베딩(RRF)"
    order = sorted(range(len(idx)), key=lambda k: -sc[k])
    return [(idx[k], sc[k]) for k in order if sc[k] > 0], used


def augment(question: str, answered: str, flat: list, picked: list, keyfn, label_fn=None, terms=None):
    """
    LLM 선별(picked) 뒤에 본문 유사도 상위 조문 중 빠진 것을 덧붙입니다.
    terms: 검색어 변환 용어(있으면 검색어에 더함). 스위치 rag_hyde 가 켜져 있으면 가상 조문도 더함.
    반환: (새 picked, 단계 표시용 dict 또는 None). 실패하면 (picked, None).
    """
    try:
        t0 = time.time()
        q = query_text(question, answered)
        tw = remember_terms(question, terms)
        if tw:
            q += "\n" + " ".join(tw)
        hy = ""
        if enabled("hyde"):
            try:
                hy = hyde(question, answered)
            except Exception:               # noqa: BLE001 — 확장 실패면 확장 없이
                hy = ""
            if hy:
                q += "\n" + hy
        ranked, used = rank(q, flat)
        used += (" + 용어" if tw else "") + (" + 가상조문" if hy else "")
        have = {keyfn(f, a) for f, a in picked}
        add = []
        for i, _s in ranked[:TOPK]:
            f, a = flat[i]
            if keyfn(f, a) not in have:
                add.append((f, a))
            if len(add) >= MAX_ADD:
                break
        lab = label_fn or (lambda f, a: f"{f.get('name', '')} {_label(a)}")
        top = [lab(*flat[i]) for i, _ in ranked[:3]]
        info = {"name": "본문 유사도 보강(v1.41)",
                "detail": (f"{used} 상위 {TOPK}개 중 선별에 없던 {len(add)}개 추가"
                           + (": " + ", ".join(lab(f, a) for f, a in add) if add else "")
                           + f" · 1~3위 {', '.join(top)} · {time.time() - t0:.2f}초"
                           + (f" · 가상조문: {hy[:80]}" if hy else ""))}
        return list(picked) + add, info
    except Exception as e:                  # noqa: BLE001
        return picked, {"name": "본문 유사도 보강 실패(기존 선별 사용)", "detail": str(e)[:200]}


def order_for_clarify(question: str, answered: str, prim: list, bps: list):
    """
    되묻기에 넘길 근거 순서(v1.40: 선별 순서대로 조문 8 + 별표 4)를 유사도 순으로 바꿉니다.
    prim/bps 는 (f, a, txt). 반환: order 목록. 실패하면 None(호출부가 기존 순서 사용).
    """
    try:
        q = query_text(question, answered)
        def _sorted(units):
            if len(units) < 2:
                return list(units)
            sc = bm25_scores(q, [f"{f.get('name', '')} {_label(a)}\n{txt}" for f, a, txt in units])
            return [units[i] for i in sorted(range(len(units)), key=lambda i: (-sc[i], i))]
        p, b = _sorted(prim), _sorted(bps)
        return p[:8] + b[:4] + p[8:] + b[4:]
    except Exception:                       # noqa: BLE001
        return None


# ── v1.41 참조 조문 확장(스위치 rag_refs) ─────────────────────────────────────────
#   기존(v1.32·v1.34): 하위법령의 '법/영 제N조' → 상위 조문, 법제처 위임 데이터 → 하위 조문.
#   여기서 더하는 두 가지(둘 다 이미 받은 조문 목록 flat 안에서만 찾음 — 법제처 호출 없음):
#     ① 같은 법령 안 참조: 본문의 "제75조제1항에 따른 …" → 같은 법령 제75조 (다른 법 이름이 바로 앞에 있으면 제외)
#     ② 위임 데이터에 없을 때 글자로 찾는 하위 조문: 법률 제N조 → 시행령·시행규칙 중 "법 제N조" 를 적은 조문
#   KoBLEX(2025): 법령 질문의 3/4이 조문 2~3개를 이어 봐야 풀림.
_REF = re.compile(r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?")


def _nk(x) -> str:
    return str(x or "").strip().lstrip("0")


def ref_expand(use: list, flat: list, have_k: set, max_add: int = 3, scan: int = 6) -> list:
    """반환: 덧붙일 (f, a) 목록. have_k 는 (법령명, 조, 가지) 집합이며 여기서 갱신합니다. 오류 시 []."""
    try:
        idx = {}
        for f, a in flat:
            if _nk(a.get("조문번호")):
                idx.setdefault((f.get("name", ""), _nk(a.get("조문번호")), _nk(a.get("조문가지번호"))), (f, a))
        out = []
        for f, a in list(use[:scan]):
            if len(out) >= max_add:
                break
            nm = f.get("name", "")
            body = a.get("조문내용", "") or ""
            me = (nm, _nk(a.get("조문번호")), _nk(a.get("조문가지번호")))
            # ① 같은 법령 안 참조 (조문 하나당 2개까지 — ② 하위 조문 자리를 남김)
            n_same = 0
            for m in _REF.finditer(body):
                if n_same >= 2:
                    break
                before = body[max(0, m.start() - 12):m.start()].rstrip()
                if before.endswith(("」", "법", "영", "규칙", "령", "같은 법", "같은 영")):
                    continue                # 다른 법령·상위/하위 법령 참조(기존 v1.32 가 처리)
                k = (nm, m.group(1), _nk(m.group(2)))
                if k == me or k in have_k or k not in idx:
                    continue
                have_k.add(k)
                out.append(idx[k])
                n_same += 1
                if len(out) >= max_add:
                    break
            # ② 법률 조문 → 그 조문을 "법 제N조" 로 적은 하위 조문(위임 데이터가 비었을 때 보완)
            if len(out) < max_add and f.get("level") == "법률" and me[1]:
                pat = re.compile(r"(?<![가-힣」])법\s*제\s*" + me[1] + r"\s*조" +
                                 (r"\s*의\s*" + me[2] if me[2] else r"(?!\s*의\s*\d)"))
                cands = []
                for g, b in flat:
                    gn = g.get("name", "")
                    if not gn.startswith(nm + " ") or not gn.endswith(("시행령", "시행규칙")):
                        continue
                    bb = b.get("조문내용", "") or ""
                    if pat.search(bb):
                        k = (gn, _nk(b.get("조문번호")), _nk(b.get("조문가지번호")))
                        # 여러 조문을 나열하는 일반 조문(민감정보 처리·권한 위임·과태료 등)은 빼고,
                        # 법 조문을 적게 언급할수록(그 조문 전용일수록) 앞에 둡니다.
                        nref = len(set(re.findall(r"법\s*제\s*(\d+)\s*조", bb)))
                        if k[1] and k not in have_k and nref <= 4:
                            cands.append((nref, len(cands), k, (g, b)))
                if cands:
                    _n, _i, k, gb = min(cands)
                    have_k.add(k)
                    out.append(gb)              # 조문 하나당 하위 1개만
        return out
    except Exception:                       # noqa: BLE001
        return []


if __name__ == "__main__":                  # 간단 자가 점검: python rag_select.py
    f = {"name": "하천법"}
    flat = [(f, {"조문번호": "33", "조문제목": "하천의 점용허가 등",
                 "조문내용": "가축을 방목하거나 사육하는 행위(등록대상동물을 위한 운동ㆍ휴식 시설을 설치하는 경우는 제외한다)"}),
            (f, {"조문번호": "1", "조문제목": "목적", "조문내용": "이 법은 하천사용의 이익을 증진하고 ..."})]
    print(json.dumps(augment("하천 둔치에 반려견 놀이터 점용허가", "", flat, [], lambda f, a: a["조문번호"])[1],
                     ensure_ascii=False))
