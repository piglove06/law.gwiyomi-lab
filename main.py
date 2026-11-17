"""
FastAPI 서버.

C# 으로 치면 Program.cs + Controller 를 합쳐놓은 파일입니다.
@app.get("/경로") 데코레이터가 [HttpGet("경로")] 어트리뷰트와 같은 역할입니다.

실행:  uvicorn main:app --reload
접속:  http://127.0.0.1:8000
"""

import asyncio
import hashlib
import hmac
import json
import unicodedata
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta, timezone
import logging
import os
import re
import secrets
import time

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-7s %(message)s",
    datefmt="%m-%d %H:%M:%S",
)
_KST = timezone(timedelta(hours=9))
for _h in logging.getLogger().handlers:
    if _h.formatter:
        _h.formatter.converter = lambda secs: datetime.fromtimestamp(secs, _KST).timetuple()
for _n in ("uvicorn", "uvicorn.access", "uvicorn.error"):
    _lg = logging.getLogger(_n)
    _lg.handlers.clear()
    _lg.propagate = True


class _QuietAccess(logging.Filter):
    """
    접근 로그에서 반복되는 정상 요청을 숨깁니다.
      /static·/favicon  화면 파일
      /api/version      화면과 자동 테스트 감시자가 수시로 부름
      /api/ask          질문은 applog 의 START/END 블록으로 따로 보여줌
    오류(4xx/5xx)는 항상 보여줍니다.
    """
    QUIET = ("/static/", "/favicon", "/api/version", "/api/ask")

    def filter(self, record):
        try:
            path, status = str(record.args[2]), int(record.args[4])
            return not (status < 400 and path.startswith(self.QUIET))
        except Exception:                                  # noqa: BLE001
            return True


logging.getLogger("uvicorn.access").addFilter(_QuietAccess())
for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Form, Request  # noqa: E402
from fastapi.responses import (  # noqa: E402
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import ai_client  # noqa: E402
import applog  # noqa: E402
import law_client  # noqa: E402
import pdf_maker  # noqa: E402
import intent as intent_mod  # noqa: E402
import law_cache  # noqa: E402
import rag_select  # noqa: E402
import answer_cache  # noqa: E402
import ai_ext  # noqa: E402
import ctx_guard  # noqa: E402

CLARIFY_ROUNDS = int(os.getenv("CLARIFY_ROUNDS", "3"))

CLARIFY_CTX_TOKENS = int(os.getenv("CLARIFY_CTX_TOKENS", "5000"))

BP_CTX_CHARS = int(os.getenv("BP_CTX_CHARS", "3500"))
TABLE_RECORDS = os.getenv("TABLE_RECORDS", "1") != "0"
BP_FETCH_CHARS = 12000
BP_SHRINK_CHARS = 1800
CLARIFY_BP_CHARS = 2500
CLARIFY_ART_CHARS = 1500
_BOX_CHARS = re.compile(r"[─━┌┐└┘├┤┬┴┼┏┓┗┛┣┫┳┻╋═║╔╗╚╝╠╣╦╩╬]+")


_CELL_END = re.compile(r"[│├┼┤]")


def _hoist_row_labels(text: str) -> str:
    """세로로 합친 첫 칸(여러 행에 걸친 구분 이름)을 그 묶음 첫 줄 앞 '▶ 이름' 한 줄로 올립니다."""
    lines = str(text or "").split("\n")
    out, group = [], []

    def flush():
        cells = []
        for ln in group:
            body = ln.lstrip()
            m = _CELL_END.search(body, 1)
            cells.append((body, m.start() if m else len(body)))
        label = ""
        for b, e in cells:
            frag = b[1:e]
            if frag.strip():
                label += (" " if label and label[-1] == " " else "") + frag.strip() + (" " if frag.endswith(" ") else "")
        if label.strip() and any(b[e:e + 1] == "├" for b, e in cells):
            out.append("▶ " + label.strip())
            out.extend("│" + b[e:] for b, e in cells)
        else:
            out.extend(group)
        group.clear()

    for ln in lines:
        if ln.lstrip().startswith("│"):
            group.append(ln)
            continue
        if group and not ln.strip():
            continue
        if group:
            flush()
        out.append(ln)
    if group:
        flush()
    return "\n".join(out)


_TBL_SEP = "│├┤┼┬┴┌┐└┘┃┣┫╋┳┻┏┓┗┛"
_TBL_DASH = "─━═"
TABLE_SNAP_COLS = 3


def _disp_w(ch: str) -> int:
    """글자의 화면 폭(칸). 한글·전각·표 그리기 문자는 2칸."""
    if ch in _TBL_SEP or ch in _TBL_DASH:
        return 2
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _tbl_segments(line: str):
    """표 한 줄 → [(시작 칸, 끝 칸, 'text'|'border', 글자)]. 표 줄이 아니면 None."""
    s = line.rstrip()
    if not s.lstrip() or s.lstrip()[0] not in _TBL_SEP:
        return None
    pos, segs, start, buf = 0, [], None, []
    for ch in s.lstrip():
        if ch in _TBL_SEP:
            if start is not None and buf:
                txt = "".join(buf)
                kind = "border" if txt.strip() and not txt.strip(_TBL_DASH + " ") else "text"
                segs.append((start, pos, kind, txt))
            pos += 2
            start, buf = pos, []
        else:
            buf.append(ch)
            pos += _disp_w(ch)
    return segs


def _listy(cell: dict) -> bool:
    """칸 안의 여러 줄이 줄바꿈된 한 문장이 아니라 따로따로인 항목들인지(끝이 아닌 줄이 칸을 다 채우지 않음)."""
    fr = [f for f in cell["lines"] if f.strip()]
    room = max(4, int(cell["w"] * 0.3))
    return any(cell["w"] - sum(_disp_w(ch) for ch in f.rstrip()) >= room for f in fr[:-1])


def _table_records(block: list):
    """
    괘선으로 그린 표(줄 목록) → 행마다 '열 이름: 값 | 열 이름: 값' 한 줄. 표 구조를 확실히 못 읽으면 None.
    세로로 합친 칸(여러 행에 걸친 구분)은 그 칸에 걸친 모든 행에 되풀이해 적습니다.
    """
    rows = [(i, _tbl_segments(ln)) for i, ln in enumerate(block) if ln.strip()]
    if any(sg is None for _, sg in rows) or len(rows) < 3:
        return None
    bounds = sorted({p for _, sg in rows for a, b, k, _ in sg if k == "border" for p in (a - 2, b)})
    if len(bounds) < 3:
        return None

    def snap(p):
        j = min(range(len(bounds)), key=lambda q: abs(bounds[q] - p))
        return j if abs(bounds[j] - p) <= TABLE_SNAP_COLS else None

    n_leaf = len(bounds) - 1
    open_c, cells, cuts, full_cuts = {}, [], [], []
    for li, sg in rows:
        covered, shift = 0, 0
        for a, b, kind, txt in sg:
            s, e = snap(a - 2 + shift), snap(b + shift)
            if s is None or e is None or e <= s:
                return None
            shift = bounds[e] - b
            if kind == "border":
                covered += e - s
                for lf in range(s, e):
                    c = open_c.pop(lf, None)
                    if c is not None:
                        c["end"] = li
                continue
            cur = open_c.get(s)
            if cur is None or cur["span"] != (s, e):
                for lf in range(s, e):
                    c = open_c.pop(lf, None)
                    if c is not None:
                        c["end"] = li
                cur = {"span": (s, e), "start": li, "end": None, "lines": [], "w": bounds[e] - bounds[s] - 2}
                cells.append(cur)
                for lf in range(s, e):
                    open_c[lf] = cur
            cur["lines"].append(txt)
        if covered:
            cuts.append(li)
            if covered >= n_leaf:
                full_cuts.append(li)
    last = rows[-1][0] + 1
    for c in cells:
        if c["end"] is None:
            c["end"] = last

    def text_of(c):
        out = ""
        for frag in c["lines"]:
            f = frag.strip()
            if not f:
                continue
            if out:
                out += " " if c["_room"] else ""
            out += f
            c["_room"] = c["w"] - sum(_disp_w(ch) for ch in frag.rstrip()) >= 2
        return re.sub(r"\s{2,}", " ", out)
    for c in cells:
        c["_room"] = True
        c["text"] = text_of(c)
    cells = [c for c in cells if c["text"]]
    if len(full_cuts) < 3:
        return None
    head_end = full_cuts[1]
    heads = [c for c in cells if c["start"] < head_end]
    body = [c for c in cells if c["start"] >= head_end]
    if not heads or not body:
        return None
    if any(not any(h["span"][0] <= lf < h["span"][1] for h in heads) for lf in range(n_leaf)) \
            or any(not re.search(r"[가-힣A-Za-z]", h["text"]) for h in heads):
        return None

    def head_of(s, e):
        names = []
        for lf in range(s, e):
            path = " ".join(h["text"] for h in sorted(heads, key=lambda h: h["start"])
                            if h["span"][0] <= lf < h["span"][1])
            if path and path not in names:
                names.append(path)
        return "/".join(names)

    edges = sorted(set(cuts) | {last})
    out = []
    for a, b in zip(edges, edges[1:]):
        if a < head_end:
            continue
        band = [c for c in body if c["start"] < b and c["end"] > a]
        if not band:
            continue
        multi = [c for c in band if c["start"] >= a and sum(1 for f in c["lines"] if f.strip()) >= 2]
        if len(multi) >= 2 and any(_listy(c) for c in multi):
            return None
        parts, seen = [], set()
        for c in sorted(band, key=lambda c: c["span"]):
            if id(c) in seen:
                continue
            seen.add(id(c))
            s, e = c["span"]
            h = "" if e - s >= n_leaf else head_of(s, e)
            if parts and h and parts[-1][0] == h:
                parts[-1] = (h, parts[-1][1] + " > " + c["text"])
            else:
                parts.append((h, c["text"]))
        out.append(" | ".join(f"{h}: {t}" if h and h != t else t for h, t in parts))
    return out or None


def _linearize_tables(text: str) -> str:
    """별표 안의 괘선 표를 행 단위 기록으로 바꿉니다. 못 읽은 표·글자가 하나라도 빠지는 표는 원문 그대로 둡니다."""
    lines = str(text or "").split("\n")
    out, i = [], 0
    while i < len(lines):
        if not (lines[i].lstrip()[:1] and lines[i].lstrip()[0] in "┌┏"):
            out.append(lines[i])
            i += 1
            continue
        j = i
        while j < len(lines) and (not lines[j].strip() or lines[j].lstrip()[0] in _TBL_SEP):
            j += 1
        while j > i and not lines[j - 1].strip():
            j -= 1
        block = lines[i:j]
        recs = None
        try:
            recs = _table_records(block)
        except Exception:                                    # noqa: BLE001
            recs = None
        if recs:
            strip = lambda t: Counter(re.sub(r"[\s" + _TBL_SEP + _TBL_DASH + "]", "", t))
            if strip("\n".join(block)) - strip("\n".join(recs)):
                recs = None
        out.extend(["(표 — 한 줄이 한 행, '열 이름: 값')"] + recs if recs else block)
        i = j
    return "\n".join(out)


def _compact_bp(text: str, records: bool = True) -> str:
    """별표 원문에서 표 테두리·정렬 공백·빈 줄을 걷어냅니다. 글자(내용)는 그대로입니다."""
    t = _BOX_CHARS.sub("", _hoist_row_labels(_linearize_tables(text) if (TABLE_RECORDS and records) else text))
    t = re.sub(r"[ \t　 ]+", " ", t)
    lines = []
    for ln in t.split("\n"):
        ln = ln.strip()
        if not ln.strip("│| "):
            continue
        lines.append(re.sub(r"\s*│\s*", "│", ln))
    return "\n".join(lines)


_KW_STOP = {"경우", "받아야", "하나요", "합니까", "인가요", "입니까", "어떻게", "무엇", "얼마", "있나요",
            "되나요", "해야", "하는", "있는", "없는", "대한", "관련", "추가", "설명", "모름", "그리고", "그런데",
            "새로", "지금", "우리", "저희", "질문", "내용", "어떤", "알려", "궁금", "때문", "정도", "이상", "미만"}


def _bp_keywords(text: str) -> list:
    """긴 별표에서 질문과 관련된 줄을 고를 때 쓸 낱말(조사·어미를 뗀 2글자 이상)."""
    out = []
    for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", str(text or "")):
        m = re.match(r"(.+?)(으로|에서|에게|까지|부터|이나|이고|이며|입니까|합니까|하나요|인가요|"
                     r"은|는|이|가|을|를|에|의|로|와|과|도|만|야|요|고)$", w)
        if m and len(m.group(1)) >= 2:
            w = m.group(1)
        m = re.match(r"(.+?)(에|에서)$", w)
        if m and len(m.group(1)) >= 2:
            w = m.group(1)
        if len(w) >= 2 and w not in _KW_STOP and not w.isdigit() and w not in out \
                and not re.match(r"^(받아|하나|무엇|모르|어느|어디|언제|그런|이런|저런|되는|하려|지으)", w):
            out.append(w)
    return out[:20]


_NOTES_HEAD = re.compile(r"^[ \t┃│|]*비\s*고", re.M)


def _fit_keep_notes(text: str, limit: int, keywords=None) -> str:
    """
    limit 자 안으로 줄이되, 표 뒤의 **비고**(보정·예외·적용 범위)는 남깁니다.
    비고는 표의 숫자를 바꾸는 규정이라 잘리면 답이 틀립니다 (예: 공사장 소음 +5dB).
    ★ v1.34 — keywords 가 있으면 앞부분 대신 **질문 낱말이 든 줄 주변**을 골라 넣습니다.
      (환경영향평가 대상사업처럼 수십 쪽짜리 별표는 앞부분만 넣으면 해당 사업 줄이 통째로 빠짐)
    """
    t = str(text or "")
    if len(t) <= limit:
        return t
    if keywords:
        lines = t.split("\n")
        df = {k: sum(1 for ln in lines if k in ln) for k in keywords}
        kws = [k for k in keywords if 0 < df[k] <= max(3, len(lines) * 0.25)]
        score = [(sum(1 for k in kws if k in ln), i) for i, ln in enumerate(lines)]
        hit = [i for sc, i in sorted(score, key=lambda x: (-x[0], x[1])) if sc > 0]
        if hit:
            keep = set(range(min(8, len(lines))))
            keep |= {i for i, ln in enumerate(lines[:15]) if re.search(r"(\d\s*지역|구\s*분|항\s*목)", ln)}
            used0 = sum(len(lines[i]) + 1 for i in keep)
            for i in hit:
                win = set(range(max(0, i - 2), min(len(lines), i + 5))) - keep
                cost = sum(len(lines[j]) + 1 for j in win)
                if used0 + cost > limit * 0.65:
                    continue
                keep |= win
                used0 += cost
            _heads = [i for i, ln in enumerate(lines) if _NOTES_HEAD.match(ln)]
            _after = [h for h in _heads if h >= hit[0]]
            ni = (_after[0] if _after else _heads[-1]) if _heads else None
            notes = "\n".join(lines[ni:ni + 25]) if ni is not None else ""
            budget = limit - min(len(notes), int(limit * 0.35)) - 40
            out, used, prev = [], 0, -1
            for i in sorted(keep):
                if ni is not None and ni <= i < ni + 25:
                    continue
                ln = lines[i]
                if used + len(ln) + 1 > budget:
                    break
                if prev >= 0 and i != prev + 1:
                    out.append("…")
                out.append(ln)
                used += len(ln) + 1
                prev = i
            body = "\n".join(out)
            if notes:
                body += "\n…\n" + notes[:int(limit * 0.35)]
            return body + "\n…(질문과 관련된 줄만 발췌)"
    mt = _NOTES_HEAD.search(t)
    i = mt.start() if mt else -1
    if i > limit * 0.4:
        notes = t[i:i + int(limit * 0.5)]
        head = t[:max(200, limit - len(notes) - 30)]
        tail = "\n…(이하 생략)" if i + len(notes) < len(t) else ""
        return head + "\n…(표 일부 생략)…\n" + notes + tail
    return t[:limit] + "\n…(이하 생략)"

_CONCL_BLOCK_RE = re.compile(r"【결론】[\s\S]*?(?=【|\Z)")


def _merge_conclusion(text: str, text2: str) -> str:
    """text 의 【결론】 블록만 text2 의 【결론】 블록으로 바꿉니다. 둘 중 하나라도 없으면 빈 문자열."""
    m1, m2 = _CONCL_BLOCK_RE.search(text or ""), _CONCL_BLOCK_RE.search(text2 or "")
    if not m1 or not m2 or len(m2.group(0).strip()) <= len("【결론】"):
        return ""
    rest = text[m1.end():]
    return text[:m1.start()] + m2.group(0).rstrip() + ("\n\n" + rest.lstrip() if rest.strip() else "")


_SELF_CORR_RE = re.compile(r"(?m)(수정된|정정된|바로잡은|다시\s*정리한)\s*결론|^\s*\**\s*(수정|정정)\s*\**\s*[:：]|정정하면"
                           r"|잠깐[,，]?\s*다시|다시\s*(확인|계산)해\s*보겠"
                           r"|결론\s*(수정|정정|변경)|재검토\s*\**\s*[:：]|최종\s*결론\s*\**\s*[:：]|이전\s*답변의?\s*결론")
_VERDICT_WORDS = re.compile(r"초과|해당|대상|위반|필요|가능")
_NEG_WORDS = re.compile(r"않|아니|아닙|아님|아닌|미달|미초과|없")
_FINAL_WORDS = re.compile(r"판정|최종|따라서|그러므로|결론적으로|이므로|므로")
_COND_WORDS = re.compile(r"경우|이면|라면|다면|만약|만일|할 때|하면")


def _verdict_conflict(text: str) -> str:
    """【결론】 첫 문장과 뒤쪽 판정(【계산】의 '판정' 줄, 없으면 【설명】의 마지막 판정 문장)이
    같은 말(초과·해당…)을 두고 긍정·부정이 반대면 그 결론 문장, 아니면 ""."""
    mc = re.search(r"【결론】\s*([^\n]+)", text or "")
    if not mc:
        return ""
    concl = re.split(r"(?<=[.다])\s", mc.group(1).strip(), 1)[0]
    later = ""
    mk = re.search(r"【계산】([\s\S]*?)(?=【|\Z)", text or "")
    if mk:
        lines = [ln for ln in mk.group(1).splitlines() if "판정" in ln]
        later = lines[-1] if lines else ""
    if not later:
        me = re.search(r"【설명】([\s\S]*?)(?=【|\Z)", text or "")
        sents = re.split(r"(?<=[.다])\s+|\n", me.group(1)) if me else []
        sents = [x for x in sents if _VERDICT_WORDS.search(x) and _FINAL_WORDS.search(x)
                 and not _COND_WORDS.search(x)]
        later = sents[-1] if sents else ""
    shared = set(_VERDICT_WORDS.findall(concl)) & set(_VERDICT_WORDS.findall(later))
    if shared and bool(_NEG_WORDS.search(concl)) != bool(_NEG_WORDS.search(later)):
        return concl
    return ""


_SELF_CORR_FIX = ("【결론】과 뒤쪽(【설명】 끝의 '수정된 결론' 등)의 판정이 다르다. 조문 원문으로 다시 따져 하나의 판정으로 정하고, "
                  "고친 흔적 없이 【결론】부터 처음부터 다시 쓴다")

ANSWER_RESERVE = int(os.getenv("ANSWER_RESERVE", "3000"))
SELECT_CTX_CAP = int(os.getenv("SELECT_CTX_CAP", "16384"))
CTX_BUDGET_MAX = int(os.getenv("CTX_BUDGET_MAX", "20000"))
QUESTION_REPEAT_TOKENS = int(os.getenv("QUESTION_REPEAT_TOKENS", "6000"))
AI_HIT_PROTECT = int(os.getenv("AI_HIT_PROTECT", "6"))
CONCL_FIX = os.getenv("CONCL_FIX", "1") != "0"
MIN_CTX_BUDGET = int(os.getenv("MIN_CTX_BUDGET", "2500"))

LAW_CANDIDATES = int(os.getenv("LAW_CANDIDATES", "3"))

LAW_ASK_SHARE = float(os.getenv("LAW_ASK_SHARE", "0.5"))
WEAK_AI_SHARE = float(os.getenv("WEAK_AI_SHARE", "0.4"))
TREE_MAX_LAWS = int(os.getenv("TREE_MAX_LAWS", "4"))
FETCH_MAX_LAWS = int(os.getenv("FETCH_MAX_LAWS", "7"))
LAW_Q = "어느 법의 규정을 찾으시나요?"
LAW_Q_DIRECT = "직접 입력 (아래 '추가로 알려줄 내용' 칸에 법령명)"
LAW_Q_UNKNOWN = "모름 (모두 찾아보기)"
_LAW_TAIL_RE = re.compile(r"(법|법률|시행령|시행규칙|규칙|령)$")


def _laws_mode(req) -> str:
    return "prefer" if str(getattr(req, "laws_mode", "") or "").lower() == "prefer" else "only"


def _split_law_names(x: str) -> list:
    """
    ★ v1.45 (C-20261028-31) — "대기환경보전법 물환경보전법"(한 칸 띄움)·"A / B" 처럼 한 칸에 여러 법을 적으면
    한 법령 이름으로 처리돼 무시됐습니다. '/' 로 나누고, 공백으로 나눈 조각이 **모두** 법령 이름 꼴(…법·…법률·…령·…규칙)
    일 때만 공백으로도 나눕니다("시행령"·"시행규칙" 조각은 앞 법에 붙임). "…에 관한 법률" 같은 긴 이름은 그대로.
    """
    out = []
    for part in re.split(r"\s*/\s*", x):
        part = part.strip().strip("「」『』\"' ")
        if not part:
            continue
        toks = []
        for t in part.split():
            if t in ("시행령", "시행규칙") and toks:
                toks[-1] += " " + t
            else:
                toks.append(t)
        if len(toks) >= 2 and all(_LAW_TAIL_RE.search(t) and len(t.split()[0]) >= 3 for t in toks):
            out.extend(toks)
        else:
            out.append(part)
    return out


def _user_laws(req) -> list:
    """사용자가 지정한 법령 이름 — 질문 화면의 "참고할 법령" 칸 + 되묻기 "어느 법" 답. 최대 3개."""
    out = []
    for x in (getattr(req, "laws", None) or []):
        x = str(x).strip().strip("「」『』\"' ")
        x = re.sub(r"\s*제\s*\d+\s*조.*$", "", x).strip().strip("「」『』\"' ")
        if x:
            out.extend(_split_law_names(x))
    ans = str(getattr(req, "answered", "") or "")
    m = re.search(re.escape(LAW_Q) + r"\s*:\s*([^/]+)", ans)
    if m:
        v = m.group(1).strip()
        if v.startswith("직접 입력"):
            for m2 in re.finditer(r"추가 설명:\s*([^/]+)", ans + " / 추가 설명: " + str(req.note or "")):
                for t in re.split(r"[,，、]|\s{2,}", m2.group(1)):
                    t = t.strip().strip("「」『』\"' ")
                    if t and _LAW_TAIL_RE.search(t):
                        out.append(t)
        elif not v.startswith("모름"):
            out.append(v.strip("「」『』\"' "))
    seen, uniq = set(), []
    for x in out:
        k = _law_key(x) if "_law_key" in globals() else x.replace(" ", "")
        if k not in seen:
            seen.add(k)
            uniq.append(x)
    return uniq[:3]

SELECT_ARTICLES = os.getenv("SELECT_ARTICLES", "1") not in ("0", "false", "False")

# 법제처 조례 검색은 다른 지자체 결과까지 섞어 돌려주므로,
# 조회 후 지자체기관명으로 한 번 더 걸러냅니다.
LOCAL_GOV = os.getenv("LOCAL_GOV", "성남시").strip()

VERSION = "1.57"

app = FastAPI(title="법령 조회 도우미", version=VERSION)

app.mount("/static", StaticFiles(directory="static"), name="static")


APP_PASSWORD = os.getenv("APP_PASSWORD", "")

SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_hex(32)

COOKIE_NAME = "lawfinder_auth"
COOKIE_DAYS = 30
PUBLIC_PATHS = {"/login", "/favicon.ico"}


def _make_token() -> str:
    """비밀번호를 서버 비밀값으로 서명한 값. 쿠키에는 이 값이 담깁니다."""
    return hmac.new(SECRET_KEY.encode(), APP_PASSWORD.encode(), hashlib.sha256).hexdigest()


def _is_logged_in(request: Request) -> bool:
    if not APP_PASSWORD:
        return True
    token = request.cookies.get(COOKIE_NAME, "")
    return hmac.compare_digest(token.encode("utf-8", "replace"), _make_token().encode("utf-8"))


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """
    모든 요청이 여기를 먼저 지나갑니다.
    ASP.NET Core 의 미들웨어 파이프라인과 같은 개념입니다.
    """
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static"):
        return await call_next(request)
    if _is_logged_in(request):
        return await call_next(request)
    if path.startswith("/api"):
        return JSONResponse({"error": "로그인이 필요합니다."}, status_code=401)
    return RedirectResponse("/login", status_code=302)


LOGIN_HTML = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>로그인 · 법령 조회 도우미</title>
<style>
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#f7f7f4;color:#1a1c20;
       font-family:-apple-system,"Segoe UI","Malgun Gothic",sans-serif}
  form{background:#fffffc;border:1px solid #d6d3c9;border-radius:3px;
       padding:30px 28px;width:320px}
  h1{font-size:16px;margin:0 0 4px}
  p{font-size:12.5px;color:#6f6d64;margin:0 0 20px}
  input{width:100%;padding:10px 12px;border:1px solid #d6d3c9;border-radius:3px;
        font:inherit;box-sizing:border-box}
  input:focus{outline:2px solid #2c4a52;outline-offset:1px;border-color:transparent}
  button{width:100%;margin-top:10px;padding:10px;border:none;border-radius:3px;
         background:#1a1c20;color:#fff;font:inherit;font-weight:600;cursor:pointer}
  button:hover{background:#000}
  .err{margin-top:12px;padding:9px 11px;border-left:3px solid #a8332c;
       background:#fdf5f4;font-size:12.5px;color:#6d2e2a}
</style></head>
<body>
<form method="post" action="/login">
  <h1>법령 조회 도우미</h1>
  <p>이용하려면 비밀번호를 입력하세요.</p>
  <input type="password" name="password" placeholder="비밀번호" autofocus required>
  <button type="submit">들어가기</button>
  __ERROR__
</form>
</body></html>"""


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _is_logged_in(request):
        return RedirectResponse("/", status_code=302)
    return LOGIN_HTML.replace("__ERROR__", "")


@app.post("/login")
def login_submit(password: str = Form(...)):
    if APP_PASSWORD and hmac.compare_digest(
            str(password or "").encode("utf-8"), APP_PASSWORD.encode("utf-8")):
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie(
            COOKIE_NAME,
            _make_token(),
            max_age=COOKIE_DAYS * 24 * 3600,
            httponly=True,
            samesite="lax",
            secure=True,
        )
        return resp
    err = '<div class="err">비밀번호가 맞지 않습니다.</div>'
    return HTMLResponse(LOGIN_HTML.replace("__ERROR__", err), status_code=401)


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp


class AskRequest(BaseModel):
    question: str
    target: str = "auto"
    skip_clarify: bool = False
    answered: str = ""
    round: int = 0
    note: str = ""
    laws: list[str] = []
    laws_mode: str = "only"
    prev_question: str = ""
    prev_laws: list[str] = []
    prev_conclusion: str = ""
    use_cache: bool = True      # v1.41 — false 면 '같은 질문 결과 불러오기'를 묻지 않음(eval_run 이 씀)


CITE_RE = re.compile(
    r"(?:((?:(?!제\s*\d)[가-힣A-Za-z0-9·ㆍ\s「」\[\]]){2,40}?)\s*)?"
    r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?"
    r"(?:\s*제\s*(\d+)\s*항)?(?:\s*제\s*(\d+)\s*호)?"
)

_SUFFIX = ["시행규칙", "시행령"]


def _norm(name: str) -> str:
    return re.sub(r"[\s「」『』\[\]()]+", "", name or "")


def _named_law(prefix: str, laws: list[dict]) -> str:
    """접두사에 법령 '이름' 이 통째로 들어 있으면 그 이름을 돌려줍니다."""
    p = _norm(prefix)
    if not p:
        return ""
    for law in sorted(laws, key=lambda x: -len(x.get("name", ""))):
        nm = _norm(law.get("name", ""))
        if nm and p.endswith(nm):
            return law["name"]
    try:
        return ctx_guard.named_by_abbr(prefix, laws)
    except Exception:                                    # noqa: BLE001
        return ""


_EXPLICIT_BRACKET = re.compile(r"[「\[]\s*([^」\]]{2,60}?)\s*[」\]]\s*$")
_EXPLICIT_WORD = re.compile(r"(?:^|[^가-힣ㆍ·])([가-힣ㆍ·]{2,40}?(?:법률|법))(?:\s*(시행령|시행규칙))?\s*$")
_NOT_LAW_NAME = {"같은법", "해당법", "관계법", "관련법", "개별법", "다른법", "특별법", "일반법", "상기법", "위의법", "모법", "이법률", "같은법률"}


def _explicit_law_name(prefix: str) -> str:
    """★ v1.44 — 인용 앞 글자가 법령명을 직접 밝혔으면 그 이름(「」·[] 안, 또는 '…법 [시행령|시행규칙]' 낱말). 아니면 ""."""
    p = (prefix or "").strip()
    m = _EXPLICIT_BRACKET.search(p)
    if m:
        return m.group(1).strip()
    m = _EXPLICIT_WORD.search(p)
    if m:
        name = m.group(1)
        if name.replace(" ", "") in _NOT_LAW_NAME or len(name.replace(" ", "")) < 3:
            return ""
        return (name + (" " + m.group(2) if m.group(2) else "")).strip()
    return ""


_BARE_LEVEL = {"시행령", "시행규칙", "같은법시행령", "같은법시행규칙", "동법시행령", "동법시행규칙"}


def _cited_by_collected(law_name: str, jo: str, gaji: str, laws: list[dict]):
    """
    ★ v1.57 — 수집하지 않은 법령의 조문 인용이, 수집한 조문 본문에 그대로 적혀 있는 것인지 찾습니다.
      예) 토양환경보전법 시행규칙 제12조가 "「위험물안전관리법」 제9조에 따른 완공검사" 라고 적고 있고 답변이 이를 옮김.
      지어낸 인용이 아니므로 '확인 안 됨'으로 찍지 않습니다(11-17 평가: 주유소·누출검사·세차장 3문항).
    있으면 (인용한 법령명, 조문 표시, 그 문장), 없으면 None. 법령명과 조 번호가 붙어 있을 때만 인정합니다.
    """
    nm = re.sub(r"[\s「」『』\[\]()·ㆍ・]+", "", law_name or "")
    if len(nm) < 3 or not str(jo).isdigit():
        return None
    pat = re.compile(r"[\s·ㆍ・]*".join(re.escape(ch) for ch in nm) + r"\s*[」』\]]?\s*제\s*" + str(jo) + r"\s*조"
                     + (rf"\s*의\s*{gaji}(?!\d)" if gaji else r"(?!\s*의\s*\d)"))
    for law in laws:
        for art in law.get("articles", []):
            body = art.get("조문내용", "") or ""
            m = pat.search(body)
            if not m:
                continue
            a = max(body.rfind("\n", 0, m.start()) + 1, m.start() - 200)
            b = body.find("\n", m.end())
            b = len(body) if b < 0 else b
            src = f"제{art.get('조문번호', '')}조" + (f"의{art['조문가지번호']}" if art.get("조문가지번호") else "")
            return law.get("name", ""), src, body[a:min(b, m.end() + 200)].strip()
    return None


def _guess_law(prefix: str, laws: list[dict], default: str) -> str:
    """
    인용문 앞에 붙은 글자에서 법령명을 추려냅니다.

    "…규정합니다. 토양환경보전법 시행령" 처럼 앞 문장이 섞여 오므로
    실제 수집한 법령 이름과 뒤에서부터 대조합니다.
    """
    p = _norm(prefix)
    if not p:
        return default
    n = _named_law(prefix, laws)
    if n:
        return n
    _ex = _explicit_law_name(prefix)
    if _norm(_ex) in _BARE_LEVEL:
        _ex = ""
    if _ex:
        _kx = re.sub(r"[\s「」『』\[\]()·ㆍ・]+", "", _ex)
        for law in laws:
            if re.sub(r"[\s「」『』\[\]()·ㆍ・]+", "", law.get("name", "")) == _kx:
                return law["name"]
        return _ex
    for suf in _SUFFIX:
        if p.endswith(suf):
            base = _norm(default)
            for s2 in _SUFFIX:
                if base.endswith(s2):
                    base = base[: -len(s2)]
                    break
            for law in laws:
                if _norm(law.get("name", "")) == base + suf:
                    return law["name"]

    m = re.search(r"(?:^|[^가-힣])(영|규칙|법)\s*$", (prefix or "").strip())
    if m:
        base = _norm(default)
        for suf in _SUFFIX:
            if base.endswith(suf):
                base = base[: -len(suf)]
                break
        want = base + {"영": "시행령", "규칙": "시행규칙", "법": ""}[m.group(1)]
        for law in laws:
            if _norm(law.get("name", "")) == want:
                return law["name"]
    return default


_HANG_MARKS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def extract_hang(body: str, hang: str = "", ho: str = "") -> str:
    """
    조문 본문에서 지정한 항(①②③)과 호(1. 2. 3.)만 잘라냅니다.

    근거 조문 카드에 "제8조제2항제4호" 를 표시할 때 조문 전체를 보여주면
    항목마다 같은 내용이 반복되어 쓸모가 없습니다.
    항 번호가 범위를 벗어나거나 못 찾으면 원문을 그대로 돌려줍니다.
    """
    if not body:
        return ""

    text = body
    if hang:
        try:
            n = int(hang)
        except ValueError:
            n = 0
        if 1 <= n <= len(_HANG_MARKS):
            mark = _HANG_MARKS[n - 1]
            nxt = _HANG_MARKS[n] if n < len(_HANG_MARKS) else None
            i = text.find(mark)
            if i >= 0:
                j = text.find(nxt, i) if nxt else -1
                text = text[i:j] if j > i else text[i:]

    if ho:
        pat = re.compile(r"(?m)^\s*" + re.escape(ho) + r"\.\s")
        line_mode = bool(pat.search(text))
        if not line_mode:
            pat = re.compile(r"(?:^|\s)" + re.escape(ho) + r"\.\s")
        mt = pat.search(text)
        if mt:
            start = mt.start()
            try:
                nxt_no = str(int(ho) + 1)
                pat2 = re.compile((r"(?m)^\s*" if line_mode else r"(?:^|\s)") + nxt_no + r"\.\s")
                mt2 = pat2.search(text, mt.end())
                text = text[start:mt2.start()] if mt2 else text[start:]
            except ValueError:
                text = text[start:]

    return text.strip()


_BP_RE = re.compile(r"(별표|별지)\s*제?\s*(\d+)")
_BP_FULL_RE = re.compile(r"(별표|별지)\s*제?\s*(\d+)(?:\s*의\s*(\d+))?")
_BP_DATE_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}\s*(?:년(?:\s*\d{1,2}\s*월(?:\s*\d{1,2}\s*일)?)?"
                         r"|[.\-/]\s*\d{1,2}\s*[.\-/]\s*\d{1,2}\.?)")


def _bp_num(sent: str):
    """★ v1.45 (C-25) — 연도·날짜를 지운 뒤 수치가 있는지."""
    return _NUM_RE.search(_BP_DATE_RE.sub(" ", sent))


def _bp_found(sent: str) -> list:
    """★ v1.45 (C-13) — 문장 속 별표 언급 → [(구분, 번호(3의2 꼴), 바로 앞 「법령명」 또는 "")]."""
    out = []
    for m in _BP_FULL_RE.finditer(sent):
        no = m.group(2) + (f"의{m.group(3)}" if m.group(3) else "")
        lm = re.search(r"「([^」]{2,60})」\s*\[?\s*$", sent[:m.start()])
        out.append((m.group(1), no, lm.group(1).strip() if lm else ""))
    return out

_NUM_RE = re.compile(
    r"\d[\d,\.]*\s*(?:"
    r"제곱미터|킬로그램|퍼센트|주일|개월|시간|리터|만원|킬로|그램|미터|%"
    r"|(?:년|월|일|주|분|초|회|차|건|명|배|톤|원)"
    r"(?:(?![가-힣])"
    r"|(?=마다|이내|이상|이하|미만|초과|간|째|씩|동안|부터|까지)"
    r"|(?=[입이은는을를에의과와로만도및]))"
    r")")

_BP_NEGATIVE = (
    "제공되지", "제공되어", "확인되지", "확인할 수 없", "확인이 어렵",
    "확정할 수 없", "판단할 수 없", "알 수 없", "가져오지 못", "받지 못",
    "포함되어 있지", "포함되지", "없으므로", "없어서", "없기 때문",
    "직접 확인", "원문이 없", "원문을 확인",
)


def _bp_used_as_basis(text: str) -> set:
    """
    답변에서 **근거로 쓰인** 별표·별지 번호를 (구분, 번호) 집합으로 돌려줍니다.

    ★ 2026-08-18 — 예전에는 본문 전체에서 "별표 4" 를 찾기만 하면 경고를
      띄웠습니다. 그런데 모델이 우리가 시킨 대로

          "「…관리지침」 별표 4 원문이 제공되지 않아 정기검사 주기를
           확정할 수 없습니다."

      라고 **부정문으로** 썼는데도 경고가 떴습니다. 답이 틀린 것처럼 보여서
      담당자가 멀쩡한 답변을 못 믿게 됩니다. (실제 2026-08-18 테스트)

    판정을 문장 단위로 바꿉니다. 별표가 나온 문장이
      · 부정·유보 문맥이면            → 경고 대상 아님 (오히려 잘 쓴 문장)
      · 수치가 하나도 없으면          → 경고 대상 아님 (틀릴 숫자가 없음)
    나머지, 즉 "별표 4에 따라 매 8년" 처럼 **번호와 수치가 같은 문장에**
    있을 때만 경고합니다.
    """
    out = set()
    sents = re.split(r"\n|(?<=다\.)\s|(?<=요\.)\s|(?<=음\.)\s", str(text or ""))
    for i, sent in enumerate(sents):
        found = _bp_found(sent)
        if not found:
            continue

        if _bp_num(sent):
            out.update(found)
            continue
        if any(w in sent for w in _BP_NEGATIVE):
            continue

        nxt = sents[i + 1] if i + 1 < len(sents) else ""
        if nxt and not _BP_RE.search(nxt) and _bp_num(nxt) \
                and not any(w in nxt for w in _BP_NEGATIVE):
            out.update(found)
    return out


_CALC_BLOCK_RE = re.compile(r"【계산】(.*?)(?=【|\Z)", re.S)
_CALC_DATE_RE = re.compile(r"(\d{4})\s*[년\-\.\/]\s*(\d{1,2})\s*[월\-\.\/]\s*(\d{1,2})\s*일?")
_CALC_PERIOD_RE = re.compile(r"(\d+)\s*(년|개월|달|주|일)")


def _add_period(y: int, m: int, d: int, n: int, unit: str) -> tuple:
    """기준일에 주기를 더합니다. 2월 29일은 말일로 눕힙니다."""
    import calendar
    if unit == "년":
        y += n
    elif unit in ("개월", "달"):
        t = (m - 1) + n
        y += t // 12
        m = t % 12 + 1
    else:
        from datetime import date, timedelta
        days = n * 7 if unit == "주" else n
        try:
            t = date(y, m, d) + timedelta(days=days)
            return t.year, t.month, t.day
        except ValueError:
            return y, m, d
    try:
        d = min(d, calendar.monthrange(y, m)[1])
    except (ValueError, calendar.IllegalMonthError):
        pass
    return y, m, d


def verify_calc(answer_text: str, user_text: str) -> list[str]:
    """
    답변의 【계산】 블록을 검산합니다. 문제가 있으면 경고 문구를 돌려줍니다.

    두 가지를 봅니다.
      (1) 기준일 + 주기 = 결과 가 실제로 맞는지
      (2) 기준일이 **사용자가 준 정보 안에 있는 날짜**인지
          (없으면 모델이 지어낸 것입니다 — 이게 제일 위험합니다)
    """
    out: list[str] = []
    mb = _CALC_BLOCK_RE.search(answer_text or "")
    if not mb:
        return out
    block = mb.group(1)

    dates = _CALC_DATE_RE.findall(block)
    periods = [(n, u) for n, u in _CALC_PERIOD_RE.findall(_CALC_DATE_RE.sub(" ", block))
               if len(n) <= 3]
    if len(dates) < 2 or not periods:
        return out

    _ri = len(dates) - 1
    _pos = [m_.start() for m_ in _CALC_DATE_RE.finditer(block)]
    _lines_at = [block.rfind("\n", 0, p_) + 1 for p_ in _pos]
    for _pat in (r"^\s*=", r"(다음|기한)"):
        _cand = [k for k, ls in enumerate(_lines_at)
                 if k > 0 and re.search(_pat, block[ls:block.find("\n", ls) if "\n" in block[ls:] else len(block)])]
        if _cand:
            _ri = _cand[0]
            break
    try:
        by, bm, bd = (int(x) for x in dates[0])
        ry, rm, rd = (int(x) for x in dates[_ri])
    except ValueError:
        return out

    plist = []
    for n_s, unit in periods[:6]:
        try:
            plist.append((int(n_s), unit))
        except ValueError:
            pass

    def _reachable(y, m, d):
        """(y,m,d) 에 plist 의 부분집합을 순서대로 더해 나올 수 있는 날짜들."""
        seen = set()
        for mask in range(1, 1 << len(plist)):
            t = (y, m, d)
            for i, (n, u) in enumerate(plist):
                if mask >> i & 1:
                    t = _add_period(*t, n, u)
            seen.add(t)
        return seen

    ok = False
    for ds in dates[:_ri]:
        try:
            start = tuple(int(x) for x in ds)
        except ValueError:
            continue
        if (ry, rm, rd) in _reachable(*start):
            ok = True
            break
    if not ok:
        pick = periods[0]
        for line in block.splitlines():
            if "주기" in line:
                m2 = _CALC_PERIOD_RE.findall(_CALC_DATE_RE.sub(" ", line))
                m2 = [(n, u) for n, u in m2 if len(n) <= 3]
                if m2:
                    pick = m2[0]
                    break
        n_s, unit = pick
        ey, em, ed = _add_period(by, bm, bd, int(n_s), unit)
        out.append(
            f"답변의 계산이 맞지 않습니다. "
            f"{by}년 {bm}월 {bd}일 + {n_s}{unit} 은 {ey}년 {em}월 {ed}일 인데 "
            f"답변은 {ry}년 {rm}월 {rd}일 이라고 적었습니다. "
            f"날짜를 그대로 쓰지 마시고 직접 확인하세요."
        )

    _udates = set()
    for ds in _CALC_DATE_RE.findall(user_text or ""):
        try:
            _udates.add(tuple(int(x) for x in ds))
        except ValueError:
            pass
    _uyears = {int(y) for y in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", user_text or "")}
    derived = False
    for ds in _CALC_DATE_RE.findall(user_text or ""):
        try:
            u = tuple(int(x) for x in ds)
        except ValueError:
            continue
        if (by, bm, bd) in _reachable(*u):
            derived = True
            break
    if (by, bm, bd) not in _udates and by not in _uyears and not derived:
        out.append(
            f"답변이 기준일을 {by}년 {bm}월 {bd}일 로 잡았지만, "
            f"질문·조건 어디에도 그 날짜가 없습니다. AI 가 지어낸 날짜입니다. "
            f"실제 시설 설치일·직전 검사일을 확인해 다시 계산하세요."
        )
    return out


def verify_citations(answer_text: str, laws: list[dict]) -> list[dict]:
    """
    AI 답변에 나온 조문 번호가 실제로 존재하는지 대조합니다.

    ★ 조문 번호만으로 찾으면 안 됩니다.
      법률 제3조와 시행령 제3조가 둘 다 있으므로, 인용문 앞의 법령명까지 봐야
      "시행령 제3조"를 법률 제3조로 오인하지 않습니다.
    """
    index = {}
    for law in laws:
        for art in law.get("articles", []):
            key = (law.get("name", ""), art.get("조문번호", ""), art.get("조문가지번호", ""))
            if key not in index:
                index[key] = (art.get("조문제목", ""), art.get("조문내용", ""),
                              law.get("level") or law.get("kind", ""))

    base_law = ""
    for lv in ("법률", "법령"):
        for law in laws:
            if (law.get("level") or law.get("kind")) == lv:
                base_law = law["name"]
                break
        if base_law:
            break
    if not base_law and laws:
        base_law = laws[0].get("name", "")

    seen = set()
    out = []
    ctx_law = ""
    for prefix, jo, gaji, hang, ho in CITE_RE.findall(answer_text):
        gaji = gaji or ""
        named = _named_law(prefix, laws)
        if named and any(_norm(l.get("name", "")) == _norm(named) for l in laws):
            ctx_law = named
        law_name = _guess_law(prefix, laws, ctx_law or base_law)

        label = f"제{jo}조" + (f"의{gaji}" if gaji else "")
        if hang:
            label += f"제{hang}항"
        if ho:
            label += f"제{ho}호"

        key = (_norm(law_name), jo, gaji, hang or "", ho or "")
        if key in seen:
            continue
        seen.add(key)

        want = ""
        for suf in _SUFFIX:
            if _norm(prefix).endswith(suf):
                want = suf
                break

        hit = index.get((law_name, jo, gaji))
        mismatch = ""
        if want and not _norm(law_name).endswith(want):
            mismatch = (f"답변은 '{want}' 이라고 했으나 실제로는 "
                        f"'{law_name}' 의 조문입니다 — 원문을 반드시 확인하세요")
        if hit is None:
            explicit = bool(named) or bool(re.search(
                r"(「[^」]+」|\[[^\]]+\]|[가-힣ㆍ·]+(법|법률|시행령|시행규칙|규칙|규정))\s*$", (prefix or "").strip()))
            for (nm, j, g), v in index.items():
                if (j, g) == (jo, gaji):
                    if _norm(nm) != _norm(law_name):
                        if explicit:
                            mismatch = (f"답변이 든 '{law_name}' {label} 은 수집한 조문에 없습니다"
                                        f"(같은 번호는 '{nm}' 에만 있음) — 근거를 원문에서 확인하세요")
                            break
                        mismatch = f"답변은 '{law_name}' 이라고 했으나 실제로는 '{nm}' 조문입니다"
                    law_name, hit = nm, v
                    break

        if hit is None and not any(_norm(l.get("name", "")) == _norm(law_name) for l in laws):
            _ref = _cited_by_collected(law_name, jo, gaji, laws)
            if _ref:
                out.append({"jo": jo, "gaji": gaji, "label": label, "ok": True, "ref": True, "law": law_name,
                            "title": f"「{_ref[0]}」 {_ref[1]} 본문이 인용한 조문 — 이 조문의 원문은 수집하지 않았습니다",
                            "text": _ref[2][:1200], "full": "", "level": "다른 법령(본문 인용)", "mismatch": ""})
                continue
        out.append(
            {
                "jo": jo, "gaji": gaji,
                "label": label,
                "ok": hit is not None,
                "law": law_name if hit else "",
                "title": hit[0] if hit else "",
                "text": (extract_hang(hit[1], hang, ho)[:1200] if hit else ""),
                "full": (hit[1][:4000] if hit else ""),
                "level": hit[2] if hit else "",
                "mismatch": mismatch,
            }
        )
    return out


_SEARCH_CACHE: dict = {}
_CACHE_TTL = 600.0


def _cache_get(key):
    hit = _SEARCH_CACHE.get(key)
    if not hit:
        return None
    if time.time() - hit[0] > _CACHE_TTL:
        _SEARCH_CACHE.pop(key, None)
        return None
    return hit[1]


def _cache_put(key, value):
    now = time.time()
    for k in [k for k, v in list(_SEARCH_CACHE.items()) if now - v[0] > _CACHE_TTL]:
        _SEARCH_CACHE.pop(k, None)
    _SEARCH_CACHE[key] = (now, value)


_NO_ANSWER_RE = re.compile(
    r"([^/]{2,60}?)\s*[:：]\s*(아니오|아니요|아니다|해당없음|해당하지\s*않음|없음|아님)")


def _excluded_terms(answered: str) -> list:
    """
    "…에 해당하나요?: 아니오" 형태에서 제외할 주제어를 뽑습니다.
    예) "방사성폐기물에 해당하나요?: 아니오"  ->  ["방사성폐기물"]
    """
    out = []
    for q, _ in _NO_ANSWER_RE.findall(answered or ""):
        if not re.search(r"(에\s*해당하나요|에\s*해당합니까|에\s*해당되나요|인가요|입니까|맞나요|인지요)\s*\??\s*$",
                         q.strip()):
            continue
        t = re.sub(r"(에\s*해당하나요|에\s*해당합니까|에\s*해당되나요|인가요|입니까|맞나요|인지요|있나요|"
                   r"하나요|받았나요|였나요)\s*\??$", "", q.strip())
        t = t.split()[-1] if t.split() else ""
        t = re.sub(r"(를|을|이|가|은|는|의)$", "", t).strip()
        if len(t) >= 2:
            out.append(t)
    return out


def _norm_q(q: str) -> str:
    """되묻기 질문 텍스트 비교용 정규화. 공백·물음표 차이는 같은 질문으로 봅니다."""
    return re.sub(r"\s+", "", str(q or "")).rstrip("?？")


def _asked_questions(answered: str) -> set:
    """
    answered 문자열("질문: 답변 / 질문: 답변 / …")에서 질문 텍스트만
    정규화해 집합으로 돌려줍니다. 이미 물은 질문을 다시 걸러내는 데 씁니다.

    ★ 로컬 모델은 프롬프트의 "이미 답변된 조건은 다시 묻지 마라" 지시를
      가끔 무시하고 직전 라운드와 완전히 같은 질문을 그대로 다시 냅니다
      (실사례: 4개 질문이 "모름"으로 답해도 3라운드 연속 토씨 하나 안 틀리고
      반복됨). 프롬프트만 믿지 않고 코드에서 한 번 더 걸러냅니다.
    """
    out = set()
    for seg in (answered or "").split(" / "):
        seg = seg.strip()
        if not seg:
            continue
        q = re.split(r"[:：]", seg, 1)[0].strip()
        if q:
            out.add(_norm_q(q))
    return out


def _is_repeat_question(q: str, already: set) -> bool:
    """
    이번 질문이 이미 물은 것인지 봅니다.

    ★ 2026-08-19 — 예전에는 질문 **전체**만 비교했습니다. 그런데 모델이
      이미 답변된 조건을 통째로 베껴 이렇게 냅니다.

          질문: "누출검사 대상인지 확인이 필요합니다.: 지하매설 저장시설"
          이미 물은 것: "누출검사 대상인지 확인이 필요합니다."

      뒤에 답이 붙어 있어서 문자열이 달라지고, 중복 판정을 빠져나가
      **같은 질문을 계속 다시 물었습니다.** (실사용 2/6 라운드에서 발생)
      → 콜론 앞부분끼리도 비교하고, 한쪽이 다른 쪽으로 시작하면
        같은 질문으로 봅니다.
    """
    n = _norm_q(q)
    if not n:
        return True
    if n in already:
        return True
    head = _norm_q(re.split(r"[:：]", str(q or ""), 1)[0])
    if head and head in already:
        return True
    for a in already:
        if len(a) >= 8 and (n.startswith(a) or a.startswith(n)):
            return True
    from difflib import SequenceMatcher
    for a in already:
        if len(a) >= 6 and len(n) >= 6:
            if a in n or n in a:
                return True
            if SequenceMatcher(None, a, n).ratio() >= 0.72:
                return True
    return False


TARGET_LABEL = {"law": "법령", "eflaw": "법령", "admrul": "행정규칙",
                "ordin": "자치법규", "expc": "법령해석례"}


def _err(msg: str):
    """
    앱 오류 응답.

    ★ 상태 코드를 200 으로 둡니다.
      Cloudflare 등 프록시가 5xx 응답 본문을 자체 HTML 오류 페이지로 교체해버려,
      실제 오류 메시지가 사용자에게 전달되지 않기 때문입니다.
      화면은 상태 코드가 아니라 error 필드를 보고 판단합니다.
    """
    return JSONResponse({"error": msg}, status_code=200)


class PdfRequest(BaseModel):
    """PDF 로 만들 조회 결과. 화면이 갖고 있는 내용을 그대로 보냅니다."""
    question: str = ""
    answered: str = ""
    answer: str = ""
    citations: list = []
    laws: list = []


@app.post("/api/pdf")
def api_pdf(req: PdfRequest):
    """
    조회 결과를 PDF 로 만들어 내려줍니다.

    브라우저 인쇄를 쓰지 않는 이유:
      인쇄 대화상자의 기본 대상이 "Microsoft Print to PDF" 인데,
      이것으로 저장하면 화면이 이미지로 렌더링되어 텍스트 복사가 안 됩니다.
      사용자가 매번 대상을 바꾸게 할 수 없으므로 서버에서 직접 만듭니다.
    """
    from fastapi.responses import Response
    try:
        data = pdf_maker.build(req.model_dump())
    except RuntimeError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"PDF 생성 실패: {e}")

    name = f"법령조회_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
    quoted = urllib.parse.quote(name)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
    )


@app.get("/api/version")
def api_version():
    """화면·서버 버전과 로컬 LLM 정보. (watch_and_test.py 가 서버 확인용으로도 부릅니다)

    ★ 2026-09-29 — 예전에는 클라우드 API 키 상태(키 끝 6자리 포함)를 돌려줬습니다.
      로그인을 없앤 뒤로 누구나 볼 수 있어서 뺐습니다.
      이 주소는 자주 불리므로 llama-server 에 새로 묻지 않고 알고 있는 값만 씁니다.
    """
    return {
        "version": VERSION,
        "llm": {
            "server": ai_client.LOCAL_SERVER,
            "model": ai_client.LOCAL_MODEL,
            "ctx": ai_client._ctx_cache.get("n") or ai_client.LOCAL_CTX_FALLBACK,
        },
    }


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/api/search")
def api_search(q: str, target: str = "law"):
    """법령명으로 목록 검색."""
    try:
        rows = law_client.search(target, q)
    except law_client.LawApiError as e:
        return _err(str(e))

    # 태그 이름이 확실치 않으므로 pick() 으로 후보를 여러 개 시도합니다.
    items = []
    for r in rows:
        items.append(
            {
                "id": law_client.pick(r, "법령ID", "행정규칙ID", "자치법규ID", "ID"),
                "name": law_client.pick(r, "법령명_한글", "법령명한글", "행정규칙명", "자치법규명"),
                "kind": law_client.pick(r, "법령구분명", "행정규칙종류", "자치법규종류"),
                "ministry": law_client.pick(r, "소관부처명", "소관부처", "지자체기관명"),
                "enforced": law_client.pick(r, "시행일자"),
                "promulgated": law_client.pick(r, "공포일자"),
                "promulgation_no": law_client.pick(r, "공포번호"),
            }
        )
    return {"count": len(items), "items": items}


@app.get("/api/detail")
def api_detail(id: str, target: str = "law"):
    """법령 본문(조문) 조회."""
    try:
        data = law_client.get_detail(target, id)
    except law_client.LawApiError as e:
        return _err(str(e))
    return data


@app.get("/api/raw", response_class=PlainTextResponse)
def api_raw(request: Request, target: str, value: str, mode: str = "search"):
    """
    원본 XML 그대로 보기.

    태그 이름을 확인할 때 쓰세요.
    예) /api/raw?target=law&value=토양환경보전법&mode=search

    ★ target·value·mode 를 뺀 나머지 쿼리스트링은 법제처로 그대로 넘어갑니다.
      문서에 없는 파라미터 이름을 시험해 볼 때 쓰세요.
      예) /api/raw?target=licbyl&value=토양환경보전법 시행규칙&search=2
          /api/raw?target=licbyl&value=x&MST=281911
    """
    #   공개 주소(Cloudflare 터널)로 들어온 요청은 cloudflared 가 같은 PC 에서 넘겨주므로 접속 주소만으로는
    #   구분이 안 돼, 터널이 붙이는 머리글(Cf-Connecting-Ip / X-Forwarded-For)이 있으면 막습니다.
    if os.getenv("RAW_API_PUBLIC", "0") != "1":
        host = (request.client.host if request.client else "") or ""
        fwd = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for")
        if fwd or host not in ("127.0.0.1", "::1", "localhost"):
            return PlainTextResponse("이 주소는 서버 PC 에서만 쓸 수 있습니다.", status_code=403)
    extra = {k: v for k, v in request.query_params.items()
             if k not in ("target", "value", "mode", "OC", "oc", "type")}
    try:
        return law_client.dump_raw(target, value, mode, extra)
    except law_client.LawApiError as e:
        return PlainTextResponse(str(e), status_code=200)


@app.post("/api/ask")
async def api_ask(req: AskRequest):
    """
    조회 결과를 NDJSON 스트림으로 흘려보냅니다.

    ★ 한 번에 응답하면 Cloudflare 가 100초에서 연결을 끊습니다(524).
      로컬 모델은 그보다 오래 걸리므로, 진행 상황을 계속 내보내
      연결을 살려둡니다. 화면에는 어느 단계인지 실시간으로 표시됩니다.
    """
    from fastapi.responses import StreamingResponse

    progress = _LoggedSteps()

    async def gen():
        loop = asyncio.get_running_loop()
        task = loop.run_in_executor(None, _ask_logged, req, progress)
        sent = 0
        idle = 0
        while not task.done():
            await asyncio.sleep(1.0)
            if sent < len(progress):
                while sent < len(progress):
                    yield json.dumps({"progress": progress[sent]},
                                     ensure_ascii=False) + "\n"
                    sent += 1
                idle = 0
            else:
                idle += 1
                if idle >= 5:
                    idle = 0
                    yield '{"ping":1}' + "\n"
        try:
            result = await task
        except Exception as e:                    # noqa: BLE001
            yield json.dumps({"error": f"처리 중 오류: {e}"},
                             ensure_ascii=False) + "\n"
            return
        while sent < len(progress):
            yield json.dumps({"progress": progress[sent]}, ensure_ascii=False) + "\n"
            sent += 1

        if isinstance(result, JSONResponse):
            try:
                result = json.loads(bytes(result.body).decode("utf-8"))
            except Exception:                     # noqa: BLE001
                result = {"error": "처리 중 오류가 발생했습니다."}
            yield json.dumps(result, ensure_ascii=False) + "\n"
            return
        try:
            yield json.dumps({"result": result}, ensure_ascii=False) + "\n"
        except (TypeError, ValueError) as e:
            yield json.dumps({"error": f"결과를 보내지 못했습니다: {e}"},
                             ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


_DOTS_RE = re.compile(r"[·ㆍ・‧∙]")


def _law_key(name: str) -> str:
    """법령명 비교용 키. 공백·가운뎃점(· / ㆍ)·하위법령 꼬리(시행령/시행규칙)를 뗍니다."""
    n = re.sub(r"\s*(시행령|시행규칙)$", "", str(name or "").strip())
    return _DOTS_RE.sub("", n).replace(" ", "")


def _stmd_rows(name: str, errs: list | None = None) -> list:
    """
    법령 체계도(lsStmd) 검색. 가운뎃점 표기가 달라 못 찾으면 바꿔서 다시 찾습니다.
    법제처 공식 이름은 "소음ㆍ진동관리법" 처럼 ㆍ(한글 아래아)를 쓰는데,
    AI 는 보통 ·(가운뎃점)으로 적습니다.
    ★ v1.45 (C-20261028-19) — errs 를 주면 법제처 호출 오류를 거기에 담습니다('0건' 과 '오류' 구분용).
    """
    tried = []
    for q in (name, name.replace("·", "ㆍ"), name.replace("ㆍ", "·"), _DOTS_RE.sub("", name)):
        if not q or q in tried:
            continue
        tried.append(q)
        try:
            rows = law_client.search("lsStmd", q)
        except law_client.LawApiError as e:
            rows = []
            if errs is not None:
                errs.append(str(e))
        if rows:
            return rows
    return []


def _pick_stmd(rows: list, name: str):
    """
    체계도 검색 결과에서 그 법령에 해당하는 줄을 고릅니다.

    ★ 2026-09-29 — 체계도 검색은 **이름 부분일치 + 가나다순**입니다.
      "폐기물관리법" 으로 찾으면 "방사성폐기물 관리법"(ㅂ)이 "폐기물관리법"(ㅍ)보다
      앞에 옵니다. 예전에는 첫 줄을 그대로 써서, AI 가 법령명을 **맞게** 추측했는데도
      방사성폐기물 관리법 체계도 위에서 답했습니다. (음식점 폐식용유·사업장 폐기물
      질문이 방사성폐기물로 빠지던 사고의 진짜 원인)
      → 이름이 정확히 같은 줄 → 그 이름으로 끝나는 가장 짧은 줄 → 가장 짧은 줄.
    """
    key = _law_key(name)
    named = [(r, _law_key(law_client.row_name("lsStmd", r))) for r in rows]
    for r, k in named:
        if k == key:
            return r
    ends = [(r, k) for r, k in named if k.endswith(key)]
    pool = ends or named
    return min(pool, key=lambda x: len(x[1]))[0] if pool else None


def _laws_by_cites(found: list, cites: list) -> list:
    """
    ★ v1.48 — 답변이 실제로 기댄 법령을 목록 맨 앞으로.
    수집 순서(지능형 검색·후보 순서)대로 돌려주면, 답은 개인정보 보호법으로 했는데 맨 앞이 민원처리법이거나(해석36)
    엉뚱한 법원조직법이 맨 앞(해석23)이었습니다. 이 순서는 화면 법령 목록, '이어서 묻기'에 넘기는 앞 답의 법령(앞 2개),
    eval '주 법령' 검사에 그대로 쓰입니다.
    정렬: 인용 확인된 횟수(법률·시행령·시행규칙은 한 묶음) 많은 순 → 답변에 먼저 나온 순 → 원래 순서.
    인용이 없으면 원래 순서 그대로. 새 목록을 만들 뿐 found(검색 캐시에 들어 있음)는 바꾸지 않습니다.
    되돌리기: 반환값의 _laws_by_cites(found, cites) 를 found 로.
    """
    def _b(n):
        return re.sub(r"\s*(시행령|시행규칙)$", "", str(n or "")).strip()
    cnt, first = {}, {}
    for i, c in enumerate(cites or []):
        if c.get("ok") and c.get("law"):
            k = _b(c["law"])
            cnt[k] = cnt.get(k, 0) + 1
            first.setdefault(k, i)
    if not cnt:
        return found
    pos = {id(f): i for i, f in enumerate(found)}
    return sorted(found, key=lambda f: (-cnt.get(_b(f.get("name")), 0), first.get(_b(f.get("name")), 10 ** 6), pos[id(f)]))


def _ctx_key(f: dict, a: dict) -> str:
    """컨텍스트 조문의 표시 키 — "토양환경보전법 시행규칙 제12조", "… [별표 4] …" (평가용)."""
    no = str(a.get("조문번호", "") or "").strip()
    gaji = str(a.get("조문가지번호", "") or "").strip()
    if no:
        no = no.lstrip("0") or no
        g = gaji.lstrip("0")
        return f"{f.get('name', '')} 제{no}조" + (f"의{g}" if g else "")
    return f"{f.get('name', '')} {str(a.get('조문제목', ''))[:40]}".strip()


HISTORY_COMPARE = os.getenv("HISTORY_COMPARE", "1") != "0"
_CHANGE_RE = re.compile(r"(바뀌|바꼈|바뀐|변경(?!\s*(신고|허가|등록|승인|사항|계약|하려|하면|할\s))|개정|달라졌|달라진|신구|전후|예전에는|이전에는|원래는|몇에서\s*몇|올랐|내렸|완화|강화)")
_QDATE_RE = re.compile(r"(\d{4})\s*[년.\-/]\s*(\d{1,2})\s*[월.\-/]\s*(\d{1,2})")
_HIST_CACHE: dict = {}


def _hist_cached(kind: str, key: str, fn):
    k = (kind, key)
    hit = _HIST_CACHE.get(k)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    val = fn()
    if val:
        now = time.time()
        for k0 in [k0 for k0, v0 in list(_HIST_CACHE.items()) if now - v0[0] >= 3600]:
            _HIST_CACHE.pop(k0, None)
        _HIST_CACHE[k] = (now, val)
    return val


def _art_id(a: dict):
    """조문·별표를 버전 사이에서 맞춰 볼 키. 조문은 (번호, 가지), 별표는 제목의 [별표 N] 머리."""
    no = str(a.get("조문번호", "") or "").lstrip("0")
    if no:
        return ("조", no, str(a.get("조문가지번호", "") or "").lstrip("0"))
    m = re.match(r"\[(별표|별지|서식)\s*(\d+)(?:의(\d+))?", str(a.get("조문제목", "")))
    return ("표", m.group(1), m.group(2) + (f"의{m.group(3)}" if m.group(3) else "")) if m else None


def _diff_lines(old: str, new: str, keywords, limit: int = 12):
    """두 본문의 줄 차이(개정 전에만 있던 줄, 개정 후에 생긴 줄). 키워드가 있으면 키워드 든 줄을 앞에 둡니다."""
    def _lines(t):
        out = []
        for ln in _compact_bp(t).replace("┃", "").split("\n"):
            ln = ln.strip(" │|")
            if ln:
                out.append(ln)
        return out

    def _norm(ln):
        ln = re.sub(r"<\s*(개정|신설|삭제|본조신설|전문개정|제목개정)[^>]*>", "", ln)
        ln = re.sub(r"[■\s]+", "", ln.replace("기후에너지환경부", "환경부"))
        return ln
    o, n = _lines(old), _lines(new)
    so, sn = {_norm(x) for x in o}, {_norm(x) for x in n}
    gone = [ln for ln in o if _norm(ln) not in sn and _norm(ln)]
    came = [ln for ln in n if _norm(ln) not in so and _norm(ln)]
    kws = [k for k in (keywords or []) if len(k) >= 2]

    def _rank(lst):
        hit = [ln for ln in lst if any(k in ln for k in kws)]
        rest = [ln for ln in lst if ln not in hit]
        return (hit + rest[:2 if hit else limit])[:limit]
    return _rank(gone), _rank(came)


def _history_block(question: str, use: list, prio: dict, keywords, steps: list, answered: str = "") -> str:
    """
    질문이 개정·변경을 물으면 [개정 이력] 텍스트를, 아니면 "" 를 돌려줍니다. 실패해도 조회는 계속합니다.
      · 질문에 날짜가 있으면 그날 공포·시행된 개정과 그 직전 버전을 비교합니다.
      · 날짜가 없으면 현행에서 한 버전씩 거슬러 올라가며(최대 6개) 지금 근거로 쓰는 조문·별표가
        **처음으로 달라지는 버전**을 찾습니다. 그 바로 다음 개정이 바꾼 것입니다.
    """
    if not HISTORY_COMPARE or not _CHANGE_RE.search(question or ""):
        return ""
    _ans_only = " ".join(re.split(r"[:：]", seg, 1)[1] for seg in (answered or "").split(" / ") if re.search(r"[:：]", seg))
    qd = _QDATE_RE.search(question or "") or _QDATE_RE.search(_ans_only)
    want_date = f"{qd.group(1)}{int(qd.group(2)):02d}{int(qd.group(3)):02d}" if qd else ""
    ordered = sorted(use, key=lambda u: prio.get(id(u[1]), 999.0))
    laws = []
    for f, _ in ordered:
        if f.get("name") and f.get("target", "law") in ("law", "eflaw") and f["name"] not in laws:
            laws.append(f["name"])

    def _d(x):
        return f"{x[:4]}.{int(x[4:6])}.{int(x[6:8])}" if len(x) == 8 else x

    texts = [str(a.get("조문내용", "")) for _, a in ordered]
    core_kw = [k for k in (keywords or []) if len(k) >= 2 and not _CHANGE_RE.search(k)
               and 0 < sum(1 for t in texts if k in t) <= 3]

    def _old(v):
        return _hist_cached("mst", v["mst"], lambda: law_client.get_detail_mst(v["mst"]))

    def _compare(nm, old, new_arts=None):
        """
        이 법령의 근거 조문(컨텍스트 상위 10개)을 old 버전과 비교 → [(라벨, 제목, 개정 전 줄, 개정 후 줄, 신설?)]
        new_arts 를 주면 그 버전 본문과, 아니면 지금 컨텍스트(현행) 본문과 비교합니다.
        질문 낱말(예: 불소)이 바뀐 줄에 든 항목을 앞에 두고 최대 4개.
        """
        idx = {}
        for a in old.get("articles") or []:
            k = _art_id(a)
            if k:
                idx.setdefault(k, a)
        nidx = {}
        for a in new_arts or []:
            k = _art_id(a)
            if k:
                nidx.setdefault(k, a)
        kws = [k for k in (keywords or []) if len(k) >= 2]
        out, n = [], 0
        for f, a in ordered:
            if f.get("name") != nm or n >= 10:
                continue
            k = _art_id(a)
            if not k:
                continue
            n += 1
            if new_arts is not None:
                a = nidx.get(k)
                if a is None:
                    continue
            label = (f"제{k[1]}조" + (f"의{k[2]}" if k[2] else "")) if k[0] == "조" else f"[{k[1]} {k[2]}]"
            oa = idx.get(k)
            if oa is None:
                out.append((label, str(a.get("조문제목", ""))[:30], [], [], True, 0))
                continue
            gone, came = _diff_lines(oa.get("조문내용", ""), a.get("조문내용", ""), keywords)
            if gone or came:
                hit = sum(1 for ln in gone + came if any(kw in ln for kw in kws))
                out.append((label, str(a.get("조문제목", ""))[:30], gone, came, False, hit))
        out.sort(key=lambda x: -x[5])
        return [x[:5] for x in out[:4]]

    if core_kw:
        def _kwn(nm):
            return sum(1 for f, a in ordered if f.get("name") == nm
                       for k in core_kw if k in str(a.get("조문내용", "")))
        laws.sort(key=lambda nm: -_kwn(nm))
    found_change = None
    _later_chg = False
    checked = []
    for nm in laws[:3]:
        try:
            hist = _hist_cached("hist", nm, lambda: law_client.get_history(nm))
        except Exception:                        # noqa: BLE001
            hist = []
        if len(hist) < 2:
            continue
        if want_date:
            for i, h in enumerate(hist[:-1]):
                if want_date in (h["prom"], h["ef"]):
                    new_arts = None if i == 0 else (_old(h).get("articles") or [])
                    diffs = _compare(nm, _old(hist[i + 1]), new_arts)
                    found_change = (nm, h, hist[i + 1], diffs)
                    break
        else:
            try:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=3) as _ex:
                    list(_ex.map(_old, hist[1:min(len(hist), 4)]))
            except Exception:                    # noqa: BLE001
                pass
            for j in range(1, min(len(hist), 7)):
                _newer = None if j == 1 else (_old(hist[j - 1]).get("articles") or [])
                diffs = _compare(nm, _old(hist[j]), _newer)
                checked.append(f"{nm} 시행 {_d(hist[j]['ef'])}")
                if diffs and (not core_kw or any(any(k in ln for k in core_kw)
                                                 for d in diffs for ln in d[2] + d[3])):
                    found_change = (nm, hist[j - 1], hist[j], diffs)
                    break
                if diffs:
                    _later_chg = True
        if found_change:
            break
    if not found_change:
        detail = (f"{_d(want_date)} 개정을 찾지 못함" if want_date
                  else f"최근 버전({', '.join(checked[-2:]) or '없음'}까지)에서 근거 조문 변경 없음")
        steps.append({"name": "개정 이력", "detail": detail})
        if want_date or not checked:
            return ""
        return ("\n\n[개정 이력 — 법제처 연혁 본문 비교]\n"
                f"· 지금 근거로 쓰는 조문·별표는 최근 {len(checked)}개 버전 동안 바뀐 줄이 없습니다.")
    nm, cur_v, prev_v, diffs = found_change
    lines = [f"「{nm}」 {_d(cur_v['prom'])} {cur_v['kind']}(제{cur_v['no']}호, 시행 {_d(cur_v['ef'])}) — "
             f"직전 버전(시행 {_d(prev_v['ef'])})과 비교"]
    if not diffs:
        lines.append("· 지금 근거로 쓰는 조문·별표는 이 개정에서 바뀐 줄이 없습니다.")
    for label, title, gone, came, is_new in diffs:
        if is_new:
            lines.append(f"· {label} {title}: 직전 버전에는 없던 조문·별표(이 개정에서 신설)")
            continue
        lines.append(f"· {label} {title} — 바뀐 줄")
        lines += [f"    개정 전: {ln[:160]}" for ln in gone]
        lines += [f"    개정 후: {ln[:160]}" for ln in came]
    if not want_date:
        lines.append("(이 개정 이후 이 조문·별표의 다른 줄도 바뀌었습니다. 현행 값은 [조문 원문] 기준)" if _later_chg else
                     "(이 개정 이후 현행까지 이 조문·별표는 더 바뀌지 않았습니다. 현행 값은 [조문 원문] 기준)")
    steps.append({"name": "개정 이력 비교",
                  "detail": f"{nm} {_d(cur_v['prom'])} {cur_v['kind']} ↔ 직전(시행 {_d(prev_v['ef'])}) · 바뀐 항목 {len(diffs)}개"})
    return "\n\n[개정 이력 — 법제처 연혁 본문 비교]\n" + "\n".join(lines)


NAMED_FIRST = os.getenv("NAMED_FIRST", "1") != "0"

_NEG_RE = re.compile(r"([가-힣A-Za-z0-9·ㆍ]{4,30}?)\s*(?:이|가)\s*아(?:니|닌|님)")


RELATED_CASES = os.getenv("RELATED_CASES", "1") != "0"
CASE_LLM_POOL = int(os.getenv("CASE_LLM_POOL", "10"))
SCOPE_CHECK = os.getenv("SCOPE_CHECK", "1") != "0"
SCOPE_TRIM = os.getenv("SCOPE_TRIM", "1") != "0"
_SCOPE_TRIM: dict = {}
_SCOPE_TRIM_TTL = 3600.0
LAW_PICK = os.getenv("LAW_PICK", "1") != "0"
CASE_IN_ANSWER = os.getenv("CASE_IN_ANSWER", "1") != "0"
ANSWER_REVIEW = os.getenv("ANSWER_REVIEW", "1") != "0"
_CASE_LABEL = {"expc": "법제처 법령해석", "prec": "판례"}
_CASE_URL = {"expc": "https://www.law.go.kr/LSW/expcInfoP.do?expcSeq={id}",
             "prec": "https://www.law.go.kr/LSW/precInfoP.do?precSeq={id}"}
_CASE_WIDE = {"경우", "여부", "관련", "대상", "기준", "절차", "방법", "신고", "규정", "해당", "의무", "처분", "허가"}


def _case_search_cached(target: str, query: str) -> list:
    key = ("case", target, query)
    hit = _cache_get(key)
    if hit is not None:
        return hit
    rows = law_client.case_search(target, query, display=50)
    if rows:
        _cache_put(key, rows)
    return rows


def _case_candidates(question: str, terms_words: list, found: list) -> list:
    """
    ★ v1.38 — 참고 사례 **후보** 모으기(API 만, 다른 스레드에서). 관련성은 답변 뒤에 LLM 이 고릅니다(_case_finalize).
    검색어 = 검색어 변환 단계의 법령 용어(LLM). v1.36~37 의 불용어 목록·점수 기준(2개 이상 겹침·3글자 전문어 등)을 대체.
    """
    from concurrent.futures import ThreadPoolExecutor
    queries = []
    for w in list(terms_words or []) + re.findall(r"[가-힣]{3,}", str(question or "")):
        w = re.sub(r"\s+", "", str(w))
        if 2 <= len(w) <= 15 and w not in _CASE_WIDE and w not in queries:
            queries.append(w)
    queries = queries[:4]
    if not queries:
        return []
    targets = ["expc", "prec"]
    cgm = ""
    for f in found[:3]:
        cgm = law_client.cgm_target(f.get("ministry", ""))
        if cgm:
            break
    if not cgm and any("환경" in str(f.get("name", "")) or "오염" in str(f.get("name", "")) for f in found[:3]):
        cgm = "meCgmExpc"
    if cgm:
        targets.insert(0, cgm)
    jobs = [(t, q) for t in targets for q in queries]
    with ThreadPoolExecutor(max_workers=6) as ex:
        res = list(ex.map(lambda tq: (tq[0], _case_search_cached(*tq)), jobs))
    qfr = {w[i:i + 2] for w in re.findall(r"[가-힣]{2,}", str(question or "")) for i in range(len(w) - 1)}
    seen, cands = set(), []
    for t, rows in res:
        for r in rows:
            tk = (t, re.sub(r"\s+", "", r["title"])[:60])
            if (t, r["id"]) in seen or tk in seen:
                continue
            seen.add((t, r["id"]))
            seen.add(tk)
            title = re.sub(r"\s+", "", r["title"])
            sc = 2 * sum(1 for q in queries if q in title) + 0.2 * sum(1 for fr in qfr if fr in title)
            if sc >= 1:
                cands.append({**r, "score": sc})
    cands.sort(key=lambda c: (-c["score"], -int(re.sub(r"\D", "", c.get("date") or "0")[:8] or 0)))
    return cands[:CASE_LLM_POOL]


def _case_finalize(question: str, cands: list) -> dict:
    """답변 뒤: LLM 이 후보 제목 중 같은 쟁점만 고르고(최대 3) 본문을 붙입니다. {"items", "detail"}."""
    from concurrent.futures import ThreadPoolExecutor
    if not cands:
        return {"items": [], "detail": "후보 없음"}
    titles = [f"[{_CASE_LABEL.get(c['target'], '중앙부처 해석')}] {c['title'][:110]}" for c in cands]
    picks = ai_client.pick_cases(question, titles)
    picked = [cands[i] for i in picks if 0 <= i < len(cands)][:3]
    if not picked:
        return {"items": [], "detail": f"후보 {len(cands)}건 — 같은 쟁점 없음(LLM 판단)"}
    def _detail(c):
        try:
            return law_client.case_detail(c["target"], c["id"])
        except Exception:                               # noqa: BLE001
            return {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        details = list(ex.map(_detail, picked))
    items = []
    for c, d in zip(picked, details):
        t = c["target"]
        date = re.sub(r"\D", "", (c.get("date") or d.get("date") or ""))[:8]
        items.append({
            "kind": _CASE_LABEL.get(t, "중앙부처 해석"),
            "title": c["title"][:150],
            "org": c.get("org", ""),
            "date": f"{date[:4]}.{date[4:6]}.{date[6:8]}" if len(date) == 8 else "",
            "q": (d.get("q") or "")[:500],
            "a": (d.get("a") or "")[:800],
            "url": _CASE_URL[t].format(id=c["id"]) if t in _CASE_URL else "",
        })
    return {"items": items,
            "detail": f"후보 {len(cands)}건 중 {len(items)}건(LLM 판단) — "
                      + " / ".join(f"{i['kind']}: {i['title'][:30]}" for i in items)}


def _negated_targets(text: str) -> list:
    """
    v1.35 — 질문자가 "○○이 아니야 / 아니라서 / 아닌데도" 라고 밝힌 대상을 뽑습니다.
    (실사용 2026-10-10: "이 부지는 토양오염관리대상시설이 아니라서" 라고 했는데, 답이 시설 특례 조문만 보고
     "관리대상시설이 아닌 부지에 대한 직접적인 규정을 확인할 수 없다" 고 함 — 일반 규정을 적용해야 함)
    """
    out = []
    for m in _NEG_RE.finditer(str(text or "")):
        noun = m.group(1)
        if re.search(r"(것|거|게|때문|경우|그런|그게|이게|정도|사실|문제|뭐|무엇)$", noun):
            continue
        if noun not in out:
            out.append(noun)
    return out[:3]


def _title_core(title: str) -> str:
    """조문 제목에서 비교용 알맹이만 — "[별표 3] 토양오염우려기준(제1조의5 관련)" → "토양오염우려기준"."""
    t = re.sub(r"^\s*\[(별표|별지|서식)[^\]]*\]\s*", "", str(title or ""))
    t = re.sub(r"\(.*?\)|<.*?>", "", t)
    return re.sub(r"[\s·ㆍ]+", "", t)


def _named_in_question(question: str, flat: list, limit: int = 4) -> list:
    """
    v1.35 — 제목(알맹이)이 "…기준" 인 조문 중 그 제목이 질문에 그대로 들어 있는 것(법률 → 시행령 → 시행규칙 순, 최대 limit 개).
    "토양오염의 우려기준" 처럼 '의' 가 끼어도 맞춥니다. 너무 짧은 제목(정의·목적 등)은 비교하지 않습니다.
    """
    qn = re.sub(r"[\s·ㆍ]+", "", str(question or ""))
    qn2 = qn.replace("의", "")
    order = {"법률": 0, "대통령령": 1, "총리령": 2, "부령": 2}
    hits = []
    for f, a in flat:
        if re.match(r"\s*\[(별지|서식)", str(a.get("조문제목", ""))):
            continue
        core = _title_core(a.get("조문제목", ""))
        if len(core.replace("의", "")) < 6 or not core.endswith("기준"):
            continue
        if core in qn or core.replace("의", "") in qn2:
            lv = f.get("level", "")
            rank = order.get(lv, 1 if str(f.get("name", "")).endswith("시행령")
                             else 2 if str(f.get("name", "")).endswith("시행규칙") else 0)
            hits.append((rank, -len(core), f, a))
    hits.sort(key=lambda x: (x[0], x[1]))
    out, seen = [], set()
    for _, _, f, a in hits:
        k = (f.get("name"), a.get("조문번호"), a.get("조문가지번호"), a.get("조문제목"))
        if k in seen:
            continue
        seen.add(k)
        out.append((f, a))
        if len(out) >= limit:
            break
    return out


def _resolve_law_exact(cands: list):
    """
    법령명 후보(긴 것부터)를 법제처 검색 결과와 **이름이 정확히 같은** 법령으로 확정합니다.
    반환: (검색 결과 행, 정식 이름) 또는 None.
    공백·가운뎃점(·/ㆍ)만 무시하고 비교합니다. "시행령/시행규칙" 꼬리는 무시하지 않습니다
    (법률 제12조와 시행규칙 제12조는 다른 조문).
    """
    LAW_T = law_client.LAW_TARGET

    def key(n):
        return _DOTS_RE.sub("", str(n or "")).replace(" ", "")

    tried = set()
    for cand in cands:
        names = [cand]
        try:
            full = law_client.resolve_abbrev(cand)
            if full and full != cand:
                names.append(full)
            _mt = re.match(r"^(.+?)\s*(시행령|시행규칙)$", str(cand).strip())
            if _mt:
                _fb = law_client.resolve_abbrev(_mt.group(1).strip())
                if _fb and _fb != _mt.group(1).strip():
                    names.append(f"{_fb} {_mt.group(2)}")
        except Exception:                                   # noqa: BLE001
            pass
        for nm in names:
            for q in (nm, nm.replace("·", "ㆍ"), nm.replace("ㆍ", "·"), _DOTS_RE.sub("", nm)):
                if not q or q in tried:
                    continue
                tried.add(q)
                try:
                    rows = law_client.search(LAW_T, q, display=100)
                except law_client.LawApiError:
                    rows = []
                for r in rows:
                    rn = law_client.row_name(LAW_T, r)
                    if key(rn) == key(nm):
                        return r, rn
    return None


def _ask_article(req, steps, it):
    """
    ★ v1.31 — 조문 직접 조회 경로 ("폐기물관리법 제25조 알려줘").
    검색어 변환·체계도 수집·선별·되묻기를 모두 건너뛰고 그 조문 하나로 답합니다.
    법령을 못 찾으면 None (일반 경로로 넘어감). 법령은 찾았는데 조문이 없으면
    "없다" 고 분명히 답합니다 (지어내지 않음).
    """
    LAW_T = law_client.LAW_TARGET
    hit = _resolve_law_exact(it.law_candidates or [it.law])
    if not hit:
        return None
    row, name = hit
    rid = law_client.row_id(LAW_T, row)
    try:
        detail = law_client.get_detail(LAW_T, rid)
    except Exception as e:                              # noqa: BLE001
        steps.append({"name": "조문 조회 실패", "detail": str(e)[:120]})
        return None

    def _no(x):
        return str(x or "").strip().lstrip("0")

    arts_all = [a for a in detail.get("articles", [])
                if a.get("조문여부") != "전문" and _no(a.get("조문번호"))]
    arts = [a for a in arts_all
            if _no(a.get("조문번호")) == it.jo and _no(a.get("조문가지번호")) == _no(it.gaji)]
    label = f"제{it.jo}조" + (f"의{it.gaji}" if it.gaji else "")
    f = {
        "id": rid, "target": LAW_T,
        "level": law_client.pick(row, "법령구분명", default="법령"),
        "mst": law_client.pick(row, "법령일련번호"),
        "name": name,
        "kind": law_client.pick(row, "법령구분명", default="법령"),
        "enforced": law_client.pick(row, "시행일자"),
        "promulgation_no": law_client.pick(row, "공포번호"),
        "ministry": law_client.pick(row, "소관부처명"),
        "meta": detail.get("meta", {}),
        "articles": arts,
    }
    steps.append({"name": "조문 직접 조회", "detail": f"「{name}」 {label}"
                  + ("" if arts else " — 해당 조문 없음")})
    if not arts:
        nums = sorted({int(_no(a.get("조문번호"))) for a in arts_all if _no(a.get("조문번호")).isdigit()})
        rng = f"제{nums[0]}조 ~ 제{nums[-1]}조" if nums else "확인 불가"
        return {"steps": steps, "laws": [f], "citations": [], "warnings": [],
                "answer": (f"【결론】\n「{name}」에서 {label}를 찾지 못했습니다. "
                           f"이 법령의 조문 범위는 {rng} 입니다. 조문 번호를 다시 확인해 주세요."),
                "intent": it.as_dict(), "debug": {"context_keys": [], "dropped_keys": []}}

    a = arts[0]
    title = a.get("조문제목", "")
    body = a.get("조문내용", "")
    context = (f"=== 여기부터는 「{name}」 조문입니다 (시행 {f.get('enforced') or '?'}) ===\n"
               f"[{name}] {label}" + (f"({title})" if title else "") + f"\n{body}")
    q = (req.question + "\n(질문 유형: 조문 조회 — 이 조문의 내용을 항·호 순서대로 쉽게 풀어 설명한다. "
         "다른 조문·하위법령·별표에 넘기는 부분은 넘긴다는 사실만 적고 내용을 지어내지 않는다)")
    try:
        text = ai_client.answer(q, context)
    except ai_client.AiError as e:
        return _err(str(e))
    if it.gaji:
        text = re.sub(rf"제{it.jo}조의(?=\s*제\d|\s*\||\s*$)", f"제{it.jo}조의{it.gaji}", text, flags=re.M)
    cites = verify_citations(text, [f])
    steps.append({"name": "인용 검증", "detail": f"{sum(1 for c in cites if c['ok'])}/{len(cites)}건 확인"})
    hitk = sorted({(c["law"], c["jo"], c["gaji"]) for c in cites if c["ok"]})
    return {
        "steps": steps, "answer": text, "laws": [f], "citations": cites, "cases": [],
        "cited_keys": [f"{n}|{j}|{g}" for n, j, g in hitk],
        "references": [], "warnings": [],
        "intent": it.as_dict(),
        "debug": {"flat_n": len(arts_all), "picked_n": 1,
                  "context_keys": [_ctx_key(f, a)], "dropped_keys": []},
    }


class _LoggedSteps(list):
    """steps.append 할 때마다 서버 로그에도 한 줄 남깁니다 (applog.step)."""

    def append(self, item):
        super().append(item)
        try:
            applog.step(item.get("name", ""), item.get("detail"))
        except Exception:                                  # noqa: BLE001
            pass


def _scope_note(req: AskRequest, result) -> None:
    """★ v1.50 — 섞인 질문에서 뺀 부분(점심 메뉴 등)은 답하지 않았다고 최종 답 끝에 한 줄 알립니다.
    되묻기 중(clarify)·오류 응답에는 붙이지 않습니다."""
    if not (SCOPE_TRIM and isinstance(result, dict)) or result.get("clarify") or not result.get("answer"):
        return
    tr = _SCOPE_TRIM.get(req.question.strip())
    if not tr or time.time() - tr[0] > _SCOPE_TRIM_TTL:
        return
    result["scope_trim"] = {"law_part": tr[1], "other": tr[2]}
    result["answer"] = (str(result["answer"]).rstrip() + "\n\n【답하지 않은 부분】\n"
                        f"질문 중 「{tr[2]}」 부분은 법령 조문으로 답할 수 있는 내용이 아니어서 답하지 않았습니다.")


def _ask_logged(req: AskRequest, progress: list):
    """
    _ask_sync 를 질문 블록 로그로 감쌉니다.

      ▶ START  새 질문(round 0)
        │ 단계들 …
        │ ? 되묻기 N차 — 사용자 응답 대기     ← 블록을 닫지 않음
        │ ↩ 되묻기 N차 응답 …                ← 같은 번호로 이어서
      ■ END    최종 답변 또는 오류
    """
    sess = applog.begin((req.question.strip(), req.target), req.question.strip(),
                        req.round, req.answered or "", req.note or "")
    try:
        _ac_res, _ac_state = answer_cache.before(req, progress, VERSION, CLARIFY_ROUNDS)
        _laws0, _mode0 = list(getattr(req, "laws", None) or []), getattr(req, "laws_mode", "")
        _q0 = req.question
        result = _ac_res if _ac_res is not None else _ask_sync(req, progress)
        if _ac_res is None:
            req.laws, req.laws_mode = _laws0, _mode0
            req.question = _q0
            _scope_note(req, result)
            answer_cache.after(req, result, _ac_state, VERSION)
    except Exception as e:
        applog.end(sess, error=f"{type(e).__name__}: {e}")
        raise
    try:
        if isinstance(result, JSONResponse):
            body = json.loads(bytes(result.body).decode("utf-8"))
            applog.end(sess, error=body.get("error") or "오류 응답")
        elif isinstance(result, dict) and result.get("clarify"):
            applog.pause(sess, result["clarify"], result.get("round"))
        elif isinstance(result, dict):
            cites = result.get("citations") or []
            ans = str(result.get("answer") or "")
            if cites:
                ok = sum(1 for c in cites if c.get("ok"))
                summary = f"답변 {len(ans):,}자 · 인용 검증 {ok}/{len(cites)}"
            elif result.get("laws") and len(ans) > 100:
                summary = f"답변 {len(ans):,}자 · 인용 없음"
            else:
                summary = f"결과: {ans[:60]}"
            if result.get("warnings"):
                summary += f" · 경고 {len(result['warnings'])}건"
            applog.end(sess, summary=summary)
        else:
            applog.clear()
    except Exception:                                      # noqa: BLE001
        applog.clear()
    return result


def _ask_sync(req: AskRequest, progress: list):
    """
    질문 → (갈래 판단) → 용어변환 → 체계도 기반 계층 검색 → 조문 → 답변 → 검증
    """
    steps = progress

    prev_note = ""
    if (req.prev_question or "").strip():
        prev_note = f"앞 질문: {req.prev_question.strip()[:200]}"
        if (req.prev_conclusion or "").strip():
            concl = re.sub(r"\s+", " ", req.prev_conclusion.strip())[:300]
            prev_note += f" / 앞 답변 결론: {concl}"
        if not req.laws and req.prev_laws:
            req.laws = [x for x in req.prev_laws if str(x).strip()][:2]
            req.laws_mode = "prefer"
        if req.round == 0:
            steps.append({"name": "앞 질문에 이어서",
                          "detail": req.prev_question.strip()[:80]
                                    + (f" · 앞 답의 법령 우선 참고: {', '.join(req.laws)}" if req.laws else "")})

    answered = " / ".join(x for x in (prev_note, req.answered, req.note.strip()) if x)

    if SCOPE_TRIM and req.round > 0:
        _tr = _SCOPE_TRIM.get(req.question.strip())
        if _tr and time.time() - _tr[0] <= _SCOPE_TRIM_TTL:
            req.question = _tr[1]

    it = intent_mod.classify(req.question)
    if req.round == 0:
        steps.append({"name": "질문 유형",
                      "detail": f"{it.label} — " + ", ".join(it.signals)[:100]})
    if it.kind == intent_mod.ARTICLE_LOOKUP and req.target in ("auto", "law"):
        direct = _ask_article(req, steps, it)
        if direct is not None:
            return direct
        steps.append({"name": "조문 직접 조회 실패", "detail": "일반 검색으로 진행합니다"})
        it = intent_mod.Intent(intent_mod.OTHER, intent_mod.LABEL[intent_mod.OTHER], True,
                               signals=["조문 직접 조회 실패"])

    if (SCOPE_CHECK and req.round == 0 and not req.answered and not req.laws
            and not (req.prev_question or "").strip()):
        _sc = ai_client.scope_split(req.question)
        ok, why = _sc["legal"], _sc["reason"]
        if ok and SCOPE_TRIM and _sc["law_part"]:
            q0 = req.question.strip()
            now = time.time()
            for k in [k for k, v in list(_SCOPE_TRIM.items()) if now - v[0] > _SCOPE_TRIM_TTL]:
                _SCOPE_TRIM.pop(k, None)
            _SCOPE_TRIM[q0] = (now, _sc["law_part"], _sc["other"])
            req.question = _sc["law_part"]
            steps.append({"name": "법령 외 부분 제외",
                          "detail": f"법령 부분만 찾습니다: {_sc['law_part'][:80]} · 뺀 부분: {_sc['other']}"})
            _it2 = intent_mod.classify(req.question)
            if _it2.kind != intent_mod.ARTICLE_LOOKUP:
                it = _it2
        if not ok:
            steps.append({"name": "법령 질문 아님", "detail": why or "법령과 무관한 질문"})
            return {
                "steps": steps, "laws": [], "citations": [], "cited_keys": [], "references": [],
                "warnings": [], "cases": [], "out_of_scope": True, "intent": it.as_dict(),
                "answer": ("【결론】\n이 도우미는 대한민국 법령·제도에 관한 질문에만 답합니다. "
                           "이 질문은 법령 조문으로 답할 수 있는 내용이 아니어서 답하지 않았습니다.\n\n"
                           "【설명】\n법령·행정 절차·자격 요건·처벌·신고·허가처럼 법령에 근거한 내용으로 바꿔 물어보시면 "
                           "관련 조문을 찾아 답해 드립니다.\n"
                           "예) \"9급 공무원 시험 응시 자격(나이·학력) 기준이 어떻게 되나요?\""),
            }

    user_laws = _user_laws(req)
    cache_key = (req.question.strip(), req.target, tuple(user_laws), _laws_mode(req) if user_laws else "",
                 (req.prev_question or "").strip()[:200])
    cached = _cache_get(cache_key) if req.round > 0 else None
    if req.round == 0:
        _SEARCH_CACHE.pop(cache_key, None)
        _SEARCH_CACHE.pop(("sel",) + cache_key, None)
        _SEARCH_CACHE.pop(("terms",) + cache_key, None)
    if cached:
        found, flat, catalog, refs, ai_hits = cached
        steps.append({"name": "이전 검색 재사용",
                      "detail": f"{len(found)}개 법령 / 조문 {len(flat)}개"})
        _t_old = _cache_get(("terms",) + cache_key)
        if isinstance(_t_old, dict):
            steps.append({"name": "검색어 변환", "detail": _t_old})
        return _ask_after_search(req, steps, answered, found, flat, catalog, refs, it, ai_hits)

    try:
        terms = ai_client.extract_terms(req.question + (f"\n(조건: {answered})" if answered else ""))
    except ai_client.AiError as e:
        return _err(str(e))
    steps.append({"name": "검색어 변환", "detail": terms})
    _cache_put(("terms",) + cache_key, terms)

    names = terms.get("법령명", [])
    words = terms.get("용어", [])

    ai_hits, ai_laws, ai_score = [], [], {}
    n_err = n_empty = 0
    ai_titles: dict = {}
    if req.target in ("auto", "law"):
        ai_rows = []
        tries = [(req.question, 0.0), (req.question, 1.0), (req.question, 3.0)]
        if words:
            tries.append((" ".join(words[:3]), 0.0))
        n_empty = 0
        n_err = 0
        for attempt, (aq, wait) in enumerate(tries, 1):
            if aq == req.question and n_empty >= 2:
                continue
            if wait:
                time.sleep(wait)
            try:
                ai_rows = law_client.ai_search(aq, display=20)
                if not ai_rows:
                    n_empty += 1
            except Exception as e:                          # noqa: BLE001
                ai_rows = []
                n_err += 1
                applog.warn(f"지능형 검색 실패({attempt}차): {e}")
            if ai_rows:
                if aq != req.question:
                    steps.append({"name": "지능형 검색(용어)", "detail": f"질문 원문으로 결과가 없어 '{aq}' 로 찾음"})
                break
        score, first = {}, {}
        for i, r in enumerate(ai_rows):
            nm = (r.get("법령명") or "").strip()
            base = re.sub(r"\s*(시행령|시행규칙)$", "", nm).strip()
            if base:
                score[base] = score.get(base, 0.0) + 1.0 / (1 + i)
                first.setdefault(base, i)
            if nm and r.get("조문번호"):
                ai_hits.append((nm, r.get("조문번호", ""), r.get("조문가지번호", "")))
                if base and len(ai_titles.get(base, [])) < 3:
                    lvl = nm[len(base):].strip()
                    ai_titles.setdefault(base, []).append(
                        (lvl + " " if lvl else "") + f"제{str(r.get('조문번호', '')).lstrip('0')}조"
                        + (f"({r.get('조문제목')})" if r.get("조문제목") else ""))
        ai_laws = sorted(score, key=lambda b: (-score[b], first[b]))
        ai_score = score
        if ai_laws:
            steps.append({"name": "지능형 검색",
                          "detail": "관련 법령(관련도 점수 순): "
                                    + ", ".join(f"{b}({score[b]:.2f})" for b in ai_laws[:5])
                                    + f" · 조문 {len(ai_hits)}개"})
        elif ai_rows == []:
            steps.append({"name": "지능형 검색", "detail": "결과 없음"})

    asp_plan = []
    asp_same = ""
    aspects = terms.get("측면") or []
    if len(aspects) >= 2 and not user_laws and req.target in ("auto", "law"):
        from concurrent.futures import ThreadPoolExecutor

        def _asp_search(a):
            g = re.sub(r"\s*(시행령|시행규칙)$", "", a.get("law", "")).strip()
            try:
                return law_client.ai_search((g + " " + a["q"]).strip(), display=10)
            except Exception as e:                      # noqa: BLE001
                applog.warn(f"측면 지능형 검색 실패: {e}")
                return []
        with ThreadPoolExecutor(max_workers=3) as ex:
            asp_rows = list(ex.map(lambda a: _asp_search({"q": a["q"], "law": ""}), aspects))
        parsed = []
        whole = [re.sub(r"\s*(시행령|시행규칙)$", "", n).strip() for n in names[:2]] + [
            b for b in ai_laws[:3] if ai_score.get(b, 0) >= 0.15 and not re.search(r"(직제|고시|훈령|예규)$", b)]
        for a, rows in zip(aspects, asp_rows):
            a_sc, a_first, hits = {}, {}, []
            for i, r in enumerate(rows):
                nm = (r.get("법령명") or "").strip()
                b = re.sub(r"\s*(시행령|시행규칙)$", "", nm).strip()
                if b:
                    a_sc[b] = a_sc.get(b, 0.0) + 1.0 / (1 + i)
                    a_first.setdefault(b, i)
                if nm and r.get("조문번호"):
                    hits.append((nm, r.get("조문번호", ""), r.get("조문가지번호", "")))
            ranked = sorted(a_sc, key=lambda b: (-a_sc[b], a_first[b]))
            guess = re.sub(r"\s*(시행령|시행규칙)$", "", a.get("law", "")).strip()
            cands = []
            for c in ([guess] if guess else []) + ranked[:3] + whole:
                if c and not any(_law_key(c) == _law_key(x) for x in cands):
                    cands.append(c)
            parsed.append({"q": a["q"], "guess": guess, "cands": cands, "hits": hits})
        chosen = ai_client.pick_aspect_laws(req.question, [{"q": p["q"], "cands": p["cands"]} for p in parsed]) \
            if any(len(p["cands"]) > 1 for p in parsed) else [p["cands"][0] if p["cands"] else "" for p in parsed]
        redo = []
        for p, law in zip(parsed, chosen):
            law = law or p["guess"] or (p["cands"][0] if p["cands"] else "")
            if not law:
                continue
            ph = [h for h in p["hits"]
                  if _law_key(re.sub(r"\s*(시행령|시행규칙)$", "", h[0])) == _law_key(law)][:3]
            note = "" if _law_key(law) == _law_key(p["guess"] or "") else f"(추측 '{p['guess']}' → LLM 선택)"
            asp_plan.append({"q": p["q"], "law": law, "backup": "", "hits": ph, "note": note})
            if not ph:
                redo.append(asp_plan[-1])
        if redo:
            with ThreadPoolExecutor(max_workers=3) as ex:
                more = list(ex.map(lambda p: _asp_search({"q": p["q"], "law": p["law"]}), redo))
            for p, rows in zip(redo, more):
                p["hits"] = [(r.get("법령명", ""), r.get("조문번호", ""), r.get("조문가지번호", "")) for r in rows
                             if r.get("조문번호") and _law_key(re.sub(r"\s*(시행령|시행규칙)$", "",
                                                                    r.get("법령명", ""))) == _law_key(p["law"])][:3]
        _seen_l = {_law_key(re.sub(r"\s*(시행령|시행규칙)$", "", (r.get("법령명") or "").strip()))
                   for rows in asp_rows for r in rows} | {_law_key(b) for b in ai_laws}
        _fake = []
        for p in list(asp_plan):
            if p["hits"] or _law_key(p["law"]) in _seen_l:
                continue
            _errs: list = []
            _rows = _stmd_rows(p["law"], _errs)
            if _errs and not _rows:
                continue
            if not any(_law_key(law_client.row_name("lsStmd", r)) == _law_key(p["law"]) for r in _rows):
                asp_plan.remove(p)
                _fake.append(p["law"])
        if _fake:
            steps.append({"name": "측면 법령 제외", "detail": f"법제처에 없는 이름이라 버림: {', '.join(_fake)}"})
        if len({_law_key(p["law"]) for p in asp_plan}) < 2:
            if len(asp_plan) >= 2:
                asp_same = asp_plan[0]["law"]
                sh = []
                for p in asp_plan:
                    for h in p["hits"]:
                        if h not in sh:
                            sh.append(h)
                ai_hits = sh + [h for h in ai_hits if h not in sh]
                steps.append({"name": "질문 쪼개기 생략",
                              "detail": f"측면 {len(asp_plan)}개가 모두 같은 법({asp_same}) — 이 법을 주 법령으로 봅니다"
                                        + (f" · 조문 {len(sh)}개 지목" if sh else "")})
            asp_plan = []
        if len(asp_plan) >= 2:
            steps.append({"name": "질문 쪼개기",
                          "detail": " / ".join(f"{i + 1}) {p['q'][:45]} → {p['law']}{p['note']} 조문 {len(p['hits'])}개"
                                               for i, p in enumerate(asp_plan))})
            inter = []
            for k in range(3):
                for p in asp_plan:
                    if k < len(p["hits"]) and p["hits"][k] not in inter:
                        inter.append(p["hits"][k])
            ai_hits = inter + [h for h in ai_hits if h not in inter]

    law_pick = []
    _abbr_full = ""
    if (LAW_PICK and not user_laws and not asp_plan and not asp_same and req.target in ("auto", "law")
            and (names or ai_laws)):
        _gk = {_law_key(re.sub(r"\s*(시행령|시행규칙)$", "", n)) for n in names}
        _tot0 = sum(ai_score.values()) or 1.0
        _sh0 = ai_score.get(ai_laws[0], 0.0) / _tot0 if ai_laws else 0.0
        if not (ai_laws and _law_key(ai_laws[0]) in _gk) and _sh0 < LAW_ASK_SHARE:
            pool = []
            for n in [re.sub(r"\s*(시행령|시행규칙)$", "", n).strip() for n in names[:3]]:
                rows = _stmd_rows(n) if n else []
                row = _pick_stmd(rows, n) if rows else None
                if row is not None and _law_key(law_client.row_name("lsStmd", row)) == _law_key(n) \
                        and not any(_law_key(n) == _law_key(x) for x in pool):
                    pool.append(n)
            for n in [b for b in ai_laws[:5]
                      if ai_score.get(b, 0) >= 0.15 and not re.search(r"(직제|고시|훈령|예규|지침)$", b)]:
                if n and not any(_law_key(n) == _law_key(x) for x in pool):
                    pool.append(n)
            if pool:
                _ait_k = {}
                for _k0, _v0 in ai_titles.items():
                    _ait_k.setdefault(_law_key(_k0), []).extend(_v0)
                picks, other, subj = ai_client.pick_main_laws(
                    req.question + (f"\n(확인된 조건: {answered})" if answered else ""), pool,
                    [("지능형 검색 조문: " + ", ".join(_ait_k[_law_key(x)])) if _ait_k.get(_law_key(x))
                     else "지능형 검색 조문 없음(이름 추측)" for x in pool])
                if other and not any(_law_key(other) == _law_key(x) for x in pool + picks):
                    rows = _stmd_rows(other)
                    row = _pick_stmd(rows, other) if rows else None
                    if row is not None and _law_key(law_client.row_name("lsStmd", row)) == _law_key(other):
                        picks = picks + [law_client.row_name("lsStmd", row)]
                    else:
                        steps.append({"name": "주 법령 선택", "detail": f"LLM 이 제시한 '{other}' 은(는) 법제처에 없어 버림"})
                law_pick = picks[:3]
                if law_pick:
                    steps.append({"name": "주 법령 선택(LLM)",
                                  "detail": f"{subj} → {', '.join(law_pick)} (후보: {', '.join(pool)})"})

    weak_ai = False
    if ai_laws and names and not user_laws and not asp_plan and not asp_same and req.target in ("auto", "law"):
        _tot = sum(ai_score.values()) or 1.0
        _share = ai_score.get(ai_laws[0], 0.0) / _tot
        _ai5 = {_law_key(x) for x in ai_laws[:5]}
        _gbase = [re.sub(r"\s*(시행령|시행규칙)$", "", n).strip() for n in names]
        if _share < WEAK_AI_SHARE and not any(_law_key(b) in _ai5 for b in _gbase):
            _real = []
            for b in _gbase:
                rows = _stmd_rows(b)
                row = _pick_stmd(rows, b) if rows else None
                if row is not None and _law_key(law_client.row_name("lsStmd", row)) == _law_key(b):
                    _real.append(b)
            if _real:
                weak_ai = True
                steps.append({"name": "지능형 검색 신뢰 낮음",
                              "detail": f"1위 '{ai_laws[0]}' 비중 {_share:.0%} 이고 AI 추측 법령 "
                                        f"'{', '.join(_real)}' 이(가) 지능형 상위 5개에 없음 — AI 추측 법령을 먼저 봅니다"})

    if (not user_laws and req.round == 0 and not req.skip_clarify and not weak_ai and not asp_plan
            and not asp_same and req.target in ("auto", "law")):
        tot = sum(ai_score.values()) or 1.0
        share = ai_score.get(ai_laws[0], 0.0) / tot if ai_laws else 0.0
        guess_keys = {_law_key(re.sub(r"\s*(시행령|시행규칙)$", "", n)) for n in names}
        ambiguous = bool(ai_laws) and share < LAW_ASK_SHARE and _law_key(ai_laws[0]) not in guess_keys
        if not ai_laws:
            real = []
            _errs19: list = []
            for n in names:
                b = re.sub(r"\s*(시행령|시행규칙)$", "", n).strip()
                rows = _stmd_rows(b, _errs19)
                if any(_law_key(law_client.row_name("lsStmd", r)) == _law_key(b) for r in rows):
                    real.append(b)
            ambiguous = not real
            if ambiguous and ((n_err and not n_empty) or _errs19):
                ambiguous = False
                steps.append({"name": "법령 확인 건너뜀",
                              "detail": "법제처 응답 오류로 법령 이름을 확인하지 못해 '어느 법?' 을 묻지 않습니다"})
        if ambiguous:
            opts = []
            for b in law_pick[:1]:
                b = re.sub(r"\s*(시행령|시행규칙)$", "", b).strip()
                if b and not any(_law_key(b) == _law_key(o) for o in opts):
                    opts.append(b)
            for b in ai_laws:
                if len(opts) >= 3:
                    break
                if any(_law_key(b) == _law_key(o) for o in opts):
                    continue
                if ai_score.get(b, 0) < 0.15 or re.search(r"(직제|고시|훈령|예규|규정|지침)$", b):
                    continue
                opts.append(b)
            for n in names:
                if len(opts) >= 4:
                    break
                b = re.sub(r"\s*(시행령|시행규칙)$", "", n).strip()
                if any(_law_key(b) == _law_key(o) for o in opts):
                    continue
                rows = _stmd_rows(b)
                row = _pick_stmd(rows, b) if rows else None
                if row is not None and _law_key(law_client.row_name("lsStmd", row)) == _law_key(b):
                    opts.append(b)
            if opts or not ai_laws:
                steps.append({"name": "법령 확인 필요",
                              "detail": (f"지능형 검색 1위 '{ai_laws[0]}' 비중 {share:.0%}, "
                                         f"AI 추측 '{', '.join(names) or '없음'}' 와 달라 어느 법인지 묻습니다"
                                         if ai_laws else
                                         f"지능형 검색 결과가 없고 AI 추측 '{', '.join(names) or '없음'}' 도 "
                                         f"법제처에 없는 이름이라 어느 법인지 묻습니다")})
                return {
                    "clarify": [{"question": LAW_Q, "options": opts + [LAW_Q_DIRECT, LAW_Q_UNKNOWN]}],
                    "round": req.round + 1,
                    "max_rounds": CLARIFY_ROUNDS + 1,
                    "answered": answered,
                    "steps": steps,
                    "intent": it.as_dict(),
                }

    if not names and not words and not ai_laws:
        return {"steps": steps, "answer": "검색어를 만들지 못했습니다. 질문을 더 구체적으로 써보세요.", "laws": []}

    found, seen = [], set()

    def add(target, query, limit=2, level="", scope=1):
        if target not in law_client.TARGETS:
            steps.append({"name": "경고", "detail": f"알 수 없는 검색 대상: {target}"})
            return
        try:
            rows = law_client.search(target, query, scope=scope)
        except law_client.LawApiError as e:
            steps.append({"name": "검색 실패", "detail": f"{target}: {e}"})
            return
        picked_n = 0
        for r in rows:
            if picked_n >= limit:
                break
            if target == "ordin" and LOCAL_GOV:
                org = law_client.pick(r, "지자체기관명", "지자체명")
                if LOCAL_GOV not in org:
                    continue

            rid = law_client.row_id(target, r)
            if not rid or rid in seen:
                continue
            seen.add(rid)
            picked_n += 1
            found.append({
                "id": rid, "target": target, "level": level,
                "mst": law_client.pick(r, "법령일련번호", "행정규칙일련번호", "자치법규일련번호"),
                "name": law_client.row_name(target, r),
                "kind": law_client.pick(r, "법령구분명", "행정규칙종류", "자치법규종류", default=TARGET_LABEL.get(target, "")),
                "enforced": law_client.pick(r, "시행일자", "회신일자"),
                "promulgation_no": law_client.pick(r, "공포번호", "발령번호", "안건번호"),
                "ministry": law_client.pick(r, "소관부처명", "지자체기관명", "회신기관명"),
            })

    LAW_T = law_client.LAW_TARGET

    if req.target in ("auto", "law"):
        user_resolved, user_bad = [], []
        for u in user_laws:
            b = re.sub(r"\s*(시행령|시행규칙)$", "", u).strip()
            hit_name = ""
            for qn in (b, ):
                rows = _stmd_rows(qn)
                exact = [r for r in rows if _law_key(law_client.row_name("lsStmd", r)) == _law_key(qn)]
                if exact:
                    hit_name = law_client.row_name("lsStmd", exact[0])
            if not hit_name:
                try:
                    full = law_client.resolve_abbrev(b)
                except Exception:                         # noqa: BLE001
                    full = b
                if full and full != b:
                    rows = _stmd_rows(full)
                    exact = [r for r in rows if _law_key(law_client.row_name("lsStmd", r)) == _law_key(full)]
                    if exact:
                        hit_name = law_client.row_name("lsStmd", exact[0])
            if hit_name:
                if not any(_law_key(hit_name) == _law_key(x) for x in user_resolved):
                    user_resolved.append(hit_name)
            else:
                user_bad.append(u)
        only_mode = _laws_mode(req) == "only"
        if not user_laws and re.search(re.escape(LAW_Q) + r"\s*:\s*직접 입력", req.answered or ""):
            steps.append({"name": "사용자 지정 법령", "detail": "직접 입력한 내용에서 법령 이름을 찾지 못해 자동으로 찾습니다 "
                                                              "(예: '토양환경보전법' 처럼 법령 이름을 적어 주세요)"})
        if user_laws:
            steps.append({"name": "사용자 지정 법령" + (" (이 법령에서만)" if only_mode else " (우선 참고)"),
                          "detail": (", ".join(user_resolved) or "확인된 법령 없음")
                                    + (f" · 찾지 못함: {', '.join(user_bad)}" if user_bad else "")
                                    + ("" if user_resolved else " — 자동 검색으로 진행")})

        cand = list(names[:2])
        if cand:
            hit = False
            for nm in cand:
                base = nm.replace(" 시행령", "").replace(" 시행규칙", "").strip()
                if _stmd_rows(base):
                    hit = True
                    break

            if not hit:
                resolved = []
                for nm in cand:
                    base = nm.replace(" 시행령", "").replace(" 시행규칙", "").strip()
                    try:
                        full = law_client.resolve_abbrev(base)
                    except Exception:
                        full = base
                    if full != base and full not in resolved:
                        resolved.append(full)
                for full in resolved:
                    try:
                        if _stmd_rows(full):
                            hit = True
                            steps.append({
                                "name": "법령명 복구(약칭)",
                                "detail": f"'{', '.join(cand)}' 을(를) 공식 약칭으로 보고 "
                                          f"'{full}' 로 재시도",
                            })
                            _ab = {_law_key(nm) for nm in cand
                                   if law_client.resolve_abbrev(nm.replace(" 시행령", "").replace(" 시행규칙", "").strip()) == full}
                            cand = [full] + [c for c in cand if _law_key(c) not in _ab]
                            _abbr_full = full
                            break
                    except law_client.LawApiError:
                        pass

            hits_all: dict[str, int] = {}
            hits_info: dict[str, int] = {}
            info_words, weak_words = [], []
            if words:
                for w in words[:3]:
                    try:
                        rows = law_client.search(LAW_T, w, display=100, scope=2)
                    except law_client.LawApiError:
                        continue
                    if not rows:
                        continue
                    informative = len(rows) < 100
                    (info_words if informative else weak_words).append(w)
                    seen_w = set()
                    for r in rows:
                        nm = law_client.row_name(LAW_T, r)
                        nm = re.sub(r"\s*(시행령|시행규칙)$", "", nm).strip()
                        if nm and nm not in seen_w:
                            seen_w.add(nm)
                            hits_all[nm] = hits_all.get(nm, 0) + 1
                            if informative:
                                hits_info[nm] = hits_info.get(nm, 0) + 1

            def _ranked(h: dict) -> list:
                return sorted(h, key=lambda n: -h[n])

            def _same(a: str, b: str) -> bool:
                return _law_key(a) == _law_key(b)

            if not hit:
                recovered = _ranked(hits_info or hits_all)
                fresh = [nm for nm in recovered if not any(_same(nm, c) for c in cand)]
                if fresh:
                    steps.append({
                        "name": "법령명 복구(본문 검색)",
                        "detail": f"'{', '.join(cand)}' 로 체계도를 찾지 못해 "
                                  f"'{', '.join(words[:3])}' 본문 검색 → "
                                  + ", ".join(fresh[:3]),
                    })
                    cand = fresh[:2] + cand
            elif hits_info and not any(_same(nm, c) for nm in hits_info for c in cand):
                ranked = _ranked(hits_info)
                best = hits_info[ranked[0]]
                tops = [n for n in ranked if hits_info[n] == best]
                if len(tops) <= 3:
                    steps.append({
                        "name": "법령명 교차검증 실패 → 본문 검색 우선",
                        "detail": f"AI 추측 '{', '.join(cand)}' 이(가) '{', '.join(info_words)}' "
                                  f"본문검색 결과에 없어 '{ranked[0]}' 을(를) 먼저 봅니다.",
                    })
                    cand = ranked[:1] + cand
                else:
                    steps.append({
                        "name": "법령명 교차검증 보류",
                        "detail": f"'{', '.join(info_words)}' 가 든 법이 {len(tops)}개로 고르게 "
                                  f"퍼져 근거가 약함 — AI 추측 '{', '.join(cand)}' 유지",
                    })

        if (asp_plan or asp_same) and not user_resolved:
            prim = []
            for x in ([asp_same] if asp_same else []) + [p["law"] for p in asp_plan] \
                    + [p["backup"] for p in asp_plan if p["backup"]] + cand:
                if x and not any(_law_key(x) == _law_key(y) for y in prim):
                    prim.append(x)
            cand = prim
        if ai_laws and not (user_resolved and only_mode) and not weak_ai and not asp_plan and not asp_same:
            top = ai_laws[0]
            if not any(_law_key(top) == _law_key(c) for c in cand):
                steps.append({"name": "법령 후보 보정(지능형 검색)",
                              "detail": f"'{top}' 을(를) 먼저 봅니다"
                                        + (f" (AI 추측: {', '.join(cand)})" if cand else "")})
            rest = [c for c in cand if _law_key(c) != _law_key(top)]
            ai_keys = {_law_key(x) for x in ai_laws[:5]}
            corr = [c for c in rest if _law_key(c) in ai_keys]
            others = [c for c in rest if _law_key(c) not in ai_keys]
            ai2 = [b for b in ai_laws[1:3]
                   if ai_score.get(b, 0) >= 0.2 and not re.search(r"(직제|고시|훈령|예규|규정)$", b)
                   and not any(_law_key(b) == _law_key(c) for c in [top] + corr)]
            _nk = {_law_key(re.sub(r"\s*(시행령|시행규칙)$", "", n)) for n in names}
            if _abbr_full:
                _nk.add(_law_key(_abbr_full))
            named_o = [c for c in others if _law_key(c) in _nk]
            others = [c for c in others if c not in named_o]
            cand = [top] + corr + named_o[:1] + ai2[:1] + [c for c in named_o[1:] + others
                                                            if not any(_law_key(c) == _law_key(x) for x in ai2[:1])]

        extra_pick = [] if user_resolved else [
            x for x in law_pick if not any(_law_key(x) == _law_key(c) for c in cand)][:1]
        if extra_pick:
            cand = cand[:1] + extra_pick + cand[1:]
            steps.append({"name": "법령 후보 추가(LLM 선택)", "detail": f"'{extra_pick[0]}' 을(를) 후보에 더합니다"})

        if user_resolved and only_mode:
            cand = list(user_resolved)
            cand_limit = len(cand)
        elif user_resolved:
            cand = list(user_resolved) + [c for c in cand
                                          if not any(_law_key(c) == _law_key(u) for u in user_resolved)]
            cand_limit = max(LAW_CANDIDATES, len(user_resolved) + 1)
        else:
            cand_limit = max(LAW_CANDIDATES, len(asp_plan)) + len(extra_pick)
        stmd_ok, stmd_fail = 0, []
        for nm in cand:
            if stmd_ok >= cand_limit:
                break
            base = nm.replace(" 시행령", "").replace(" 시행규칙", "").strip()
            rows = _stmd_rows(base)
            row = _pick_stmd(rows, base) if rows else None
            if row is None:
                stmd_fail.append(f"{base}(체계도 검색 결과 없음)")
                continue
            mst = law_client.pick(row, "법령일련번호")
            if not mst:
                stmd_fail.append(f"{base}(법령일련번호 없음)")
                continue
            try:
                tree = law_client.get_hierarchy(mst)
            except law_client.LawApiError as e:
                stmd_fail.append(f"{base}(체계도 조회 실패: {str(e)[:60]})")
                continue
            stmd_ok += 1
            frags = set()
            for w in (names + words):
                w = re.sub(r"(법|시행령|시행규칙)$", "", w)
                for i in range(len(w) - 1):
                    frags.add(w[i:i + 2])

            def _score(ar):
                nm = ar.get("name", "")
                return sum(1 for fr in frags if fr in nm)

            tree_laws = [L for L in tree["laws"] if L["id"] and L["id"] not in seen]
            if len(tree_laws) > TREE_MAX_LAWS:
                own = {_law_key(base), _law_key(base + " 시행령"), _law_key(base + " 시행규칙")}
                core = [L for L in tree_laws if L.get("level") == "법률" or _law_key(L["name"]) in own]
                wfr = {w2[i:i + 2] for w2 in (re.sub(r"\s+", "", w) for w in words) for i in range(len(w2) - 1)}

                def _wscore(L):
                    return sum(1 for fr in wfr if fr in L.get("name", ""))
                more = sorted([L for L in tree_laws if L not in core and _wscore(L) > 0], key=lambda L: -_wscore(L))
                kept = (core + more)[:max(TREE_MAX_LAWS, len(core))]
                steps.append({"name": "체계도 축소",
                              "detail": f"{base}: 하위법령 {len(tree_laws)}개 중 {len(kept)}개만 — "
                                        + ", ".join(L["name"] for L in kept)})
                tree_laws = kept
            for L in tree_laws:
                seen.add(L["id"])
                # mst(법령일련번호) 는 법제처 3단비교 URL 의 lsiSeq 로 씁니다.
                found.append({**L, "target": LAW_T, "kind": L["level"], "ministry": ""})

            ranked = sorted(tree["admruls"], key=lambda x: -_score(x))
            picked_admruls = [a for a in ranked if _score(a) > 0][:4]

            for A in picked_admruls:
                if A["id"] not in seen:
                    seen.add(A["id"])
                    found.append({
                        "id": A["id"], "target": "admrul",
                        "level": f"{A['level']} 위임", "name": A["name"],
                        "kind": A["kind"], "enforced": A["enforced"],
                        "promulgation_no": A["promulgation_no"], "ministry": "",
                    })
        if asp_plan:
            for f in found:
                f["_aspects"] = [p["q"] for p in asp_plan]
        if stmd_fail:
            applog.warn("법령 후보 체계도 실패: " + ", ".join(stmd_fail))
            steps.append({"name": "법령 후보 일부 실패",
                          "detail": ", ".join(stmd_fail[:3])
                                    + (" — 다음 후보로 대신했습니다" if stmd_ok else "")})

    if not found:
        if req.target in ("auto", "law"):
            for q in (names + words)[:3]:
                add(LAW_T, q, 2, "법령")
            if not found:
                for q in (words + names)[:2]:
                    add(LAW_T, q, 2, "법령", scope=2)
                if found:
                    steps.append({"name": "본문 검색으로 확보",
                                  "detail": ", ".join(f["name"] for f in found[:4])})
        if req.target in ("auto", "admrul"):
            for q in (names + words)[:2]:
                add("admrul", q, 2, "행정규칙")
    if req.target == "ordin":
        gov = LOCAL_GOV or ""
        for q in (words or names)[:2]:
            q = q.strip()
            add("ordin", q if (gov and gov in q) else f"{gov} {q}".strip(), 2, "자치법규")
    if req.target == "expc":
        for q in (names + words)[:3]:
            add("expc", q, 3, "해석례")

    steps.append({"name": "법령 검색", "detail": [f"{f.get('level') or f.get('kind')}: {f['name']}" for f in found]})
    if not found:
        return {"steps": steps, "answer": "해당하는 법령을 찾지 못했습니다.", "laws": []}

    refs = {}
    _fetch = sorted(found, key=lambda f: (0 if f.get("level") in ("법률", "시행령", "시행규칙") else 1))
    if asp_plan:
        _fetch = sorted(found, key=lambda f: (0 if f.get("level") == "법률" else
                                              1 if f.get("level") in ("시행령", "시행규칙") else 2))
    _nfetch = FETCH_MAX_LAWS + 2 + (3 if asp_plan else 0)
    _fetched = {id(x) for x in _fetch[:_nfetch]}
    for f in _fetch[:_nfetch]:
        try:
            detail = law_cache.get_detail(f["target"], f["id"], f.get("mst", ""), f.get("enforced", ""),
                                          lambda: law_client.get_detail(f["target"], f["id"]))
        except Exception as e:                            # noqa: BLE001
            if not isinstance(e, law_client.LawApiError):
                applog.warn(f"조문 수집 실패({f.get('name', '')}) — 그 법령만 건너뜁니다: {type(e).__name__}: {e}")
            continue
        f["meta"] = detail["meta"]
        f["articles"] = detail["articles"]

        if f.get("level") == "법률" and f.get("id"):
            f["delegated"] = law_client.delegation_map(f["id"])

        if f["target"] == "ordin":
            real = detail["meta"].get("자치법규명", "")
            if real and f["name"] and real.strip() != f["name"].strip():
                f["articles"] = []
                f["warning"] = f"조회 결과가 '{real}' 로 나와 제외했습니다."
                continue
            org = detail["meta"].get("지자체기관명", "")
            if LOCAL_GOV and org and LOCAL_GOV not in org:
                f["articles"] = []
                f["warning"] = f"{org} 조례여서 제외했습니다 (설정: {LOCAL_GOV})."
                continue

        for r in law_client.extract_references(detail["articles"], f["name"]):
            refs[r] = refs.get(r, 0) + 1

    flat = []
    for f in [x for x in found if id(x) in _fetched]:
        for a in f.get("articles", []):
            if a.get("조문여부") == "전문":
                continue
            if not (a.get("조문내용") or "").strip():
                continue
            flat.append((f, a))

    if not flat:
        return {"steps": steps, "answer": "조문 본문을 가져오지 못했습니다.", "laws": found}

    def label_of(a):
        no, gaji = a.get("조문번호", ""), a.get("조문가지번호", "")
        title = a.get("조문제목", "")
        if not no:
            return title or "(제목 없음)"
        return f"제{no}조" + (f"의{gaji}" if gaji else "") + (f"({title})" if title else "")

    catalog = "\n".join(
        f"{i+1}. [{f['name']}] {label_of(a)}" for i, (f, a) in enumerate(flat)
    )

    _cache_put(cache_key, (found, flat, catalog, refs, ai_hits))
    return _ask_after_search(req, steps, answered, found, flat, catalog, refs, it, ai_hits)


def _ai_hit_articles(ai_hits, use: list) -> list:
    """지능형 검색 상위 AI_HIT_PROTECT 개가 가리키는 조문 중 use 에 들어 있는 것 [(f, a)]."""
    def _n(x):
        return str(x or "").strip().lstrip("0")
    out = []
    for law, jo, gaji in (ai_hits or [])[:AI_HIT_PROTECT]:
        for f, a in use:
            if (str(law).replace(" ", "") == f["name"].replace(" ", "")
                    and _n(a.get("조문번호")) == _n(jo) and _n(a.get("조문번호"))
                    and _n(a.get("조문가지번호")) == _n(gaji)):
                out.append((f, a))
                break
    return out


def _ask_after_search(req, steps, answered, found, flat, catalog, refs, it=None, ai_hits=None):
    """검색이 끝난 뒤의 단계. 되묻기 라운드마다 여기부터 다시 실행됩니다."""
    excluded = _excluded_terms(answered)
    if excluded:
        dropped = [f for f in found
                   if any(x in f.get("name", "") for x in excluded)]
        if dropped and len(dropped) < len(found):
            drop_ids = {id(d) for d in dropped}
            found = [f for f in found if id(f) not in drop_ids]
            flat = [(f, a) for f, a in flat if id(f) not in drop_ids]
            catalog = "\n".join(
                f"{i+1}. [{f['name']}] "
                + (lambda no, gaji, t: (f"제{no}조" + (f"의{gaji}" if gaji else "")
                                        + (f"({t})" if t else "")) if no else (t or "(제목 없음)"))(
                    a.get("조문번호", ""), a.get("조문가지번호", ""), a.get("조문제목", ""))
                for i, (f, a) in enumerate(flat))
            steps.append({
                "name": "제외 조건 적용",
                "detail": f"'{', '.join(excluded)}' 아님 → "
                          + ", ".join(d["name"] for d in dropped[:4]) + " 제외",
            })

    def label_of(a):
        no, gaji = a.get("조문번호", ""), a.get("조문가지번호", "")
        title = a.get("조문제목", "")
        if not no:
            return title or "(제목 없음)"
        return f"제{no}조" + (f"의{gaji}" if gaji else "") + (f"({title})" if title else "")

    if it is None:
        it = intent_mod.classify(req.question)
    MAX_ROUNDS = CLARIFY_ROUNDS
    bp_kw = _bp_keywords(req.question + " " + (answered or ""))

    def _akey(f, a):
        return (f["name"], str(a.get("조문번호", "")), str(a.get("조문가지번호", "")),
                a.get("조문제목", ""))

    _ul = _user_laws(req)
    sel_key = ("sel", req.question.strip(), req.target, tuple(_ul), _laws_mode(req) if _ul else "",
               (req.prev_question or "").strip()[:200])
    picked = None
    cached_sel = _cache_get(sel_key) if req.round > 0 else None
    if cached_sel:
        idx = {_akey(f, a): (f, a) for f, a in flat}
        picked = [idx[k] for k in cached_sel if k in idx] or None
        if picked:
            steps.append({"name": "조문 선별 재사용",
                          "detail": f"이전 라운드에서 고른 {len(picked)}개"})
    if picked is None and SELECT_ARTICLES and len(flat) > 12:
        try:
            cat_sel = catalog
            q_sel = req.question + (f"\n(사용자가 참고 법령으로 지정: {', '.join(_ul)} — 이 법령의 조문을 우선 고려)"
                                    if _ul else "")
            n_sel = ai_client.count_tokens(ai_client.select_prompt(q_sel, catalog))
            lim = min(ai_client.server_ctx(), SELECT_CTX_CAP) - ai_client.MAXTOK_SELECT - 64
            if n_sel > lim:
                out_l, prev_nm = ["(법령명은 묶음 첫 줄 [ ] 에 한 번만 적었다. 그 아래 번호들은 그 법령의 조문이다)"], None
                for i, (f, a_) in enumerate(flat):
                    if f["name"] != prev_nm:
                        out_l.append(f"[{f['name']}]")
                        prev_nm = f["name"]
                    out_l.append(f"{i + 1}. {label_of(a_)}")
                compact = "\n".join(out_l)
                n_c = ai_client.count_tokens(ai_client.select_prompt(q_sel, compact))
                if n_c > lim:
                    compact = "\n".join(ln for ln in out_l
                                         if not re.match(r"^\d+\.\s*\[(별지|서식)", ln))
                    n_c = ai_client.count_tokens(ai_client.select_prompt(q_sel, compact))
                steps.append({"name": "조문 목록 압축",
                              "detail": f"선별 입력 {n_sel:,}토큰 > 한도 {lim:,} — 법령명 묶음 표기로 {n_c:,}토큰"})
                catalog_for_cut, n_sel = compact, n_c
                cat_sel = compact
            else:
                catalog_for_cut = catalog
            if n_sel > lim:
                lines = catalog_for_cut.splitlines()
                keep_n = max(20, int(len(lines) * lim / n_sel * 0.95))
                cat_sel = "\n".join(lines[:keep_n])
                steps.append({"name": "조문 목록 축소",
                              "detail": f"선별 입력 {n_sel:,}토큰 > 한도 {lim:,} — "
                                        f"목록 {len(lines)}줄 중 앞 {keep_n}줄만 보냄"})
            nums = ai_client.select_articles(q_sel, cat_sel)
            picked = [flat[n - 1] for n in nums if 1 <= n <= len(flat)]
            _seen_n: set = set()
            picked = [x for x in picked if not (id(x[1]) in _seen_n or _seen_n.add(id(x[1])))]
            if not picked:
                steps.append({"name": "조문 선별", "detail": "선별 결과 없음(응답 형식 오류) — 전체 조문을 씁니다"})
                picked = None
        except ai_client.AiError as e:
            applog.warn(f"조문 선별 실패 — 전체 조문을 씁니다: {e}")
            picked = None
        if picked:
            steps.append({"name": "조문 선별",
                          "detail": f"{len(flat)}개 중 {len(picked)}개 선택"})
        if ai_hits and picked is not None:
            def _n(x):
                return str(x or "").strip().lstrip("0")
            have = {_akey(f, a) for f, a in picked}
            extra, front, seen = [], [], set()
            for law, jo, gaji in ai_hits[:6]:
                for f, a in flat:
                    if (_law_key(f["name"]) == _law_key(law) and law.replace(" ", "") == f["name"].replace(" ", "")
                            and _n(a.get("조문번호")) == _n(jo) and _n(a.get("조문가지번호")) == _n(gaji)):
                        k = _akey(f, a)
                        if k in seen:
                            break
                        seen.add(k)
                        front.append((f, a))
                        if k not in have:
                            extra.append((f, a))
                        break
            if front:
                picked = front + [p for p in picked if _akey(*p) not in seen]
            if extra:
                steps.append({"name": "지능형 검색 조문 추가",
                              "detail": ", ".join(_ctx_key(f, a) for f, a in extra)})
            moved = [p for p in front if p not in extra]
            if moved:
                steps.append({"name": "지능형 검색 조문 우선",
                              "detail": ", ".join(_ctx_key(f, a) for f, a in moved)})
        if picked:
            _cache_put(sel_key, [_akey(f, a) for f, a in picked])

    if picked and rag_select.enabled("select"):
        _tw_rag = next((s_.get("detail", {}).get("용어", []) for s_ in steps
                        if s_.get("name") == "검색어 변환" and isinstance(s_.get("detail"), dict)), [])
        picked, _rag_info = rag_select.augment(req.question, answered or "", flat, picked, _akey,
                                               label_fn=lambda f_, a_: _ctx_key(f_, a_), terms=_tw_rag)
        if _rag_info:
            steps.append(_rag_info)
    if picked and ai_ext.enabled():
        try:
            _x_notes = []
            if req.round > 0 and answered:
                picked, _xa = ai_ext.merge(picked, ai_ext.answer_hits(req.question, answered, flat), _akey, front=True)
                if _xa:
                    _x_notes.append("답 반영 재검색 " + ", ".join(_ctx_key(f, a) for f, a in _xa))
            picked, _xb = ai_ext.merge(picked, ai_ext.admrul_hits(req.question, flat), _akey, front=False)
            if _xb:
                _x_notes.append("행정규칙 " + ", ".join(_ctx_key(f, a) for f, a in _xb))
            picked, _xc = ai_ext.merge(picked, ai_ext.byl_hits(req.question, flat), _akey, front=False)
            if _xc:
                _x_notes.append("별표 " + ", ".join(_ctx_key(f, a) for f, a in _xc))
            if _x_notes:
                steps.append({"name": "지능형 검색 보강", "detail": " / ".join(_x_notes)})
        except Exception as e:                            # noqa: BLE001
            applog.warn(f"지능형 검색 보강 실패 — 건너뜁니다: {e}")
    use = list(picked or flat)


    named_ids: set = set()
    if picked is not None and NAMED_FIRST:
        named = _named_in_question(req.question, flat)
        if named:
            have_n = {_akey(f, a) for f, a in use}
            add_n = [p for p in named if _akey(*p) not in have_n]
            use = use + add_n
            named_ids = {id(a) for _, a in named}
            steps.append({"name": "질문에 이름이 나온 조문 보호",
                          "detail": ", ".join(_ctx_key(f, a) for f, a in named)
                                    + (f" (추가 {len(add_n)}개)" if add_n else "")})
    _cited_ids: set = set()
    if picked is not None and ctx_guard.enabled("cited"):
        try:
            _cited = ctx_guard.cited_in_question(req.question, flat)
        except Exception as e:                            # noqa: BLE001
            applog.warn(f"질문에 적힌 조문 찾기 실패 — 건너뜁니다: {e}")
            _cited = []
        if _cited:
            have_c = {_akey(f, a) for f, a in use}
            add_c = [p for p in _cited if _akey(*p) not in have_c]
            use = use + add_c
            _cited_ids = {id(a) for _, a in _cited}
            steps.append({"name": "질문에 적힌 조문 보호",
                          "detail": ", ".join(_ctx_key(f, a) for f, a in _cited)
                                    + (f" (추가 {len(add_c)}개)" if add_c else "")})

    parent_rank: dict = {}
    if picked:
        def _nk(x):
            return str(x or "").strip().lstrip("0")
        flat_idx = {}
        for f, a in flat:
            if _nk(a.get("조문번호")):
                flat_idx.setdefault((f["name"], _nk(a.get("조문번호")), _nk(a.get("조문가지번호"))), (f, a))
        have_k = {(f["name"], _nk(a.get("조문번호")), _nk(a.get("조문가지번호"))) for f, a in use}
        parents = []
        for i_use, (f, a) in enumerate(list(use)):
            nm = f.get("name", "")
            if not nm.endswith(("시행령", "시행규칙")) or len(parents) >= 4:
                continue
            fam = re.sub(r"\s*(시행령|시행규칙)$", "", nm).strip()
            body = a.get("조문내용", "") or ""
            for m in re.finditer(r"(?<![가-힣「」])(법|영)\s*제\s*(\d+)\s*조(?:\s*의\s*(\d+))?", body):
                if body[max(0, m.start() - 3):m.start()].endswith("같은 "):
                    continue
                tgt = fam if m.group(1) == "법" else fam + " 시행령"
                k = (tgt, m.group(2), _nk(m.group(3)))
                if k in have_k or k not in flat_idx:
                    continue
                have_k.add(k)
                parents.append(flat_idx[k])
                parent_rank[id(flat_idx[k][1])] = i_use + 0.3
                if len(parents) >= 4:
                    break
        if parents:
            use = use + parents
            steps.append({"name": "상위 조문 자동 포함",
                          "detail": ", ".join(_ctx_key(f, a) for f, a in parents)})

        children = []
        for i_use, (f, a) in enumerate(list(use[:8])):
            if f.get("level") != "법률" or len(children) >= 4:
                continue
            dl = (f.get("delegated") or {}).get(
                law_client.dele_key(a.get("조문번호", ""), a.get("조문가지번호", ""))) or []
            for d in dl:
                if d.get("_kind_raw") == "인용법령" or not d.get("_jo"):
                    continue
                k = (str(d.get("_title") or "").strip(), _nk(d.get("_jo")), _nk(d.get("_jo_gaji")))
                if k in have_k or k not in flat_idx:
                    continue
                have_k.add(k)
                children.append(flat_idx[k])
                parent_rank[id(flat_idx[k][1])] = i_use + 0.4
                if len(children) >= 4:
                    break
        if children:
            use = use + children
            steps.append({"name": "위임 조문 자동 포함",
                          "detail": ", ".join(_ctx_key(f, a) for f, a in children)})

        if rag_select.enabled("refs"):
            _refs = rag_select.ref_expand(use, flat, have_k)
            if _refs:
                use = use + _refs
                steps.append({"name": "참조 조문 자동 포함(v1.41)",
                              "detail": ", ".join(_ctx_key(f, a) for f, a in _refs)})

    def _bp_no(a):
        """별표 항목에서 번호를 뽑습니다. 조문제목이 '[별표 4] …' 형태입니다."""
        mt = re.match(r"\[(별표|별지|서식)\s*(\d+)(?:의(\d+))?", a.get("조문제목", ""))
        return (mt.group(1), mt.group(2) + (f"의{mt.group(3)}" if mt.group(3) else "")) if mt else None

    _known = {f["name"] for f in found}
    wanted = set()
    prio = {id(a): float(i) for i, (f, a) in enumerate(use)}
    prio.update(parent_rank)
    cite_rank: dict = {}
    _cite_src: dict = {}
    for i_use, (f, a) in enumerate(use):
        body = a.get("조문내용", "")
        for mt in re.finditer(r"(별표|별지)\s*제?\s*(\d+)(?:\s*의\s*(\d+))?\s*호?", body):
            kind = mt.group(1)
            no = mt.group(2) + (f"의{mt.group(3)}" if mt.group(3) else "")
            owner = f["name"]
            near = body[max(0, mt.start() - 100):mt.start()]
            names = re.findall(r"「([^」]{2,60})」", near)
            if names:
                cand = names[-1].strip()
                if cand in _known:
                    owner = cand
                elif cand != f["name"]:
                    continue
            wanted.add((owner, kind, no))
            cite_rank.setdefault((owner, kind, no), i_use)
            _cite_src.setdefault((owner, kind, no), []).append(id(a))
    _neg_early = [re.sub(r"\s+", "", n) for n in _negated_targets(req.question)]
    if _neg_early:
        demoted = []
        for f, a in use:
            t = re.sub(r"\s+", "", str(a.get("조문제목", "")))
            if any(n in t for n in _neg_early) and id(a) in prio:
                prio[id(a)] += 100.0
                demoted.append(_ctx_key(f, a))
        if demoted:
            steps.append({"name": "아니라고 밝힌 대상의 특례 조문 뒤로", "detail": ", ".join(demoted[:6])})
    named_bp: set = set()
    if named_ids:
        for i_use, (f, a) in enumerate(use):
            if id(a) not in named_ids:
                continue
            prio[id(a)] = min(prio.get(id(a), 0.0), 0.2)
            for mt in re.finditer(r"(별표|별지)\s*제?\s*(\d+)(?:\s*의\s*(\d+))?", a.get("조문내용", "")):
                k3 = (f["name"], mt.group(1), mt.group(2) + (f"의{mt.group(3)}" if mt.group(3) else ""))
                named_bp.add(k3)
                if k3 in cite_rank:
                    cite_rank[k3] = min(cite_rank[k3], 0.6)
    for f, a in use:
        if id(a) in _cited_ids:
            prio[id(a)] = min(prio.get(id(a), 0.0), 0.2)
    for f, a in _ai_hit_articles(ai_hits, use):
        prio[id(a)] = min(prio.get(id(a), 0.0), 0.3)
    for k3, ids in _cite_src.items():
        pr = min(prio.get(i, 999.0) for i in ids)
        cite_rank[k3] = min(pr, 0.6) if k3 in named_bp else pr
    for f, a in use:
        k = _bp_no(a)
        if k and (f["name"], *k) in cite_rank:
            prio[id(a)] = min(prio[id(a)], cite_rank[(f["name"], *k)] - 0.5)

    missing_bp: list[dict] = []

    if wanted:
        already = {(f["name"], *(_bp_no(a) or ("", "")))for f, a in use}
        added = []
        for f, a in flat:
            key = _bp_no(a)
            if not key:
                continue
            if (f["name"], key[0], key[1]) in wanted and (f["name"], *key) not in already:
                added.append((f, a))
        if added:
            use = use + added
            for f, a in added:
                k = _bp_no(a)
                prio[id(a)] = cite_rank.get((f["name"], *k), len(use)) - 0.5
            steps.append({"name": "별표 자동 포함",
                          "detail": ", ".join(a.get("조문제목", "")[:30] for _, a in added[:5])})

        # 법령(target=law)은 본문 조회에 별표가 아예 딸려오지 않습니다.
        got = already | {(f["name"], *(_bp_no(a) or ("", ""))) for f, a in added}
        still = sorted(w for w in wanted if w not in got)
        if still:
            by_name = {}
            for f, _ in use:
                by_name.setdefault(f["name"], f)
            for f in found:
                by_name.setdefault(f["name"], f)
            fetched = []
            for law_name, kind, no in still:
                f = by_name.get(law_name)
                if not f:
                    continue
                try:
                    rows = law_client.get_byeolpyo(
                        f.get("target", "law"), f.get("mst") or f.get("id", ""), law_name)
                except Exception:
                    rows = []
                row = next((r for r in rows
                            if (r.get("no", "") + (f"의{r['gaji']}" if r.get("gaji") else "")) == no
                            and (r.get("kind") or "별표") == kind), None)
                link = (row or {}).get("link", "")
                title = (row or {}).get("title", "")
                if row and row.get("body"):
                    body = row["body"][:BP_FETCH_CHARS]
                    fetched.append(f"{law_name} {kind} {no}")
                elif kind == "별지":
                    body = (f"[{kind} {no}] {title}\n"
                            f"(제출용 서식입니다. 양식 자체는 주기·기준을 정하지 않습니다.)")
                else:
                    body = (f"[{kind} {no}] {title}\n"
                            f"({kind} {no} 의 제목은 위와 같습니다. 그러나 본문은 법제처가 "
                            f"API 로 제공하지 않아 가져오지 못했습니다. "
                            f"이 {kind} 가 정한 주기·기간·수치·기준은 확인할 수 없습니다. "
                            f"절대 추측하거나 기억으로 채우지 말고, "
                            f"'{kind} {no}({title}) 원문을 확인해야 한다' 고 답하십시오.)")
                    missing_bp.append({"law": law_name, "kind": kind, "no": no,
                                       "title": title, "link": link})
                art = {
                    "조문번호": "", "조문가지번호": "",
                    "조문제목": f"[{kind} {no}] {title}".strip(),
                    "조문여부": "조문", "시행일자": "", "구분": kind,
                    "조문내용": body, "파일링크": link,
                }
                arts = f.setdefault("articles", [])
                if not any(x.get("조문제목") == art["조문제목"] for x in arts):
                    arts.append(art)
                use.append((f, art))
                prio[id(art)] = cite_rank.get((law_name, kind, no), len(use)) - 0.5
            if fetched:
                steps.append({"name": "별표 API 조회", "detail": ", ".join(fetched)})
            if missing_bp:
                steps.append({
                    "name": "별표 본문 미확보",
                    "detail": ", ".join(f"{m['law']} {m['kind']} {m['no']}" for m in missing_bp)
                              + " — 첨부파일로만 제공되어 수치 확인 불가",
                })

    delegated_hits = 0
    units = []
    for f, a in use:
        body = a.get("조문내용", "")
        if a.get("구분") in ("별표", "별지", "서식") or \
                str(a.get("조문제목", "")).startswith(("[별표", "[별지", "[서식")):
            lim = BP_CTX_CHARS
            if (f["name"], *(_bp_no(a) or ("", ""))) in named_bp:
                lim = min(BP_CTX_CHARS, 1600)
            _form = a.get("구분") in ("별지", "서식") or str(a.get("조문제목", "")).startswith(("[별지", "[서식"))
            body = _fit_keep_notes(_compact_bp(body, records=not _form), lim, bp_kw)

        dele = (f.get("delegated") or {}).get(
            law_client.dele_key(a.get("조문번호", ""), a.get("조문가지번호", "")))
        if dele:
            def _cite(d):
                title = d.get("_title") or d.get("위임법령제목", "")
                if not title:
                    return ""
                jo = (d.get("_jo") or "").lstrip("0")
                gaji = (d.get("_jo_gaji") or "").lstrip("0")
                if jo:
                    return f"「{title}」 제{jo}조" + (f"의{gaji}" if gaji else "")
                return f"「{title}」({d.get('_kind_raw') or d.get('_kind') or '위임'}, 조문번호 미제공)"

            dele_hints, ref_hints = [], []
            for d in dele:
                c = _cite(d)
                if not c:
                    continue
                if d.get("_kind_raw") == "인용법령":
                    if len(ref_hints) < 3 and c not in ref_hints:
                        ref_hints.append(c)
                elif len(dele_hints) < 4 and c not in dele_hints:
                    dele_hints.append(c)

            note = ""
            if dele_hints:
                note += ("\n(※ 법제처 위임법령 데이터: 이 조문은 "
                         + ", ".join(dele_hints) +
                         "에 위임됩니다. 하위법령을 인용할 때는 위 이름과 "
                         "조문번호를 그대로 쓰고, 다른 번호를 짐작해서 쓰지 "
                         "마십시오. '조문번호 미제공' 이라고 적힌 것은 "
                         "조문번호를 빼고 이름만 쓰십시오.")
            if ref_hints:
                note += (("\n(※ " if not note else " 또한 ")
                         + "법제처 데이터상 이 조문이 참조하는 조문: "
                         + ", ".join(ref_hints)
                         + " — 이것은 위임이 아니라 상호참조이므로 "
                           "'하위법령' 이라고 쓰지 마십시오.")
            if note:
                body += note + ")"
                delegated_hits += 1

        units.append((f, a, f"[{f['name']}] {label_of(a)}\n{body}"))
    if delegated_hits:
        steps.append({"name": "위임법령 힌트 추가",
                      "detail": f"법률 조문 {delegated_hits}개에 위임 조문번호 힌트 붙임"})

    def _is_bp(u):
        a = u[1]
        return a.get("구분") in ("별표", "별지", "서식") or \
            str(a.get("조문제목", "")).startswith(("[별표", "[별지", "[서식"))

    if not req.skip_clarify and req.round == 0 and not it.needs_facts:
        steps.append({"name": "되묻기 생략",
                      "detail": f"질문 유형 '{it.label}' — 사례 사실 없이 답할 수 있음"})
    n_facts = len(intent_mod._CASE_FACTS.findall(req.question))
    max_r = min(MAX_ROUNDS, 1) if n_facts >= 2 else MAX_ROUNDS
    if LAW_Q in (answered or ""):
        max_r += 1
    if not req.skip_clarify and req.round < max_r and it.needs_facts:
        prim = [u for u in units if not _is_bp(u)]
        bps = [u for u in units if _is_bp(u)]
        order = prim[:8] + bps[:4] + prim[8:] + bps[4:]
        if rag_select.enabled("clarify"):
            order = rag_select.order_for_clarify(req.question, answered or "", prim, bps) or order
            steps.append({"name": "되묻기 근거 유사도 정렬(v1.41)", "detail": " / ".join(_ctx_key(f_, a_) for f_, a_, _ in order[:4])})
        parts, used = [], 0
        for f, a, txt in order:
            piece = _fit_keep_notes(txt, CLARIFY_BP_CHARS if _is_bp((f, a, txt)) else CLARIFY_ART_CHARS,
                                    bp_kw if _is_bp((f, a, txt)) else None)
            if used + len(piece) > CLARIFY_CTX_TOKENS * 2 and parts:
                break
            parts.append(piece)
            used += len(piece)
        clar_ctx = "\n\n".join(parts)
        n_cl = ai_client.count_tokens(clar_ctx)
        if n_cl > CLARIFY_CTX_TOKENS:
            clar_ctx = clar_ctx[:int(len(clar_ctx) * CLARIFY_CTX_TOKENS / n_cl * 0.95)] \
                + "\n…(분량 제한으로 이하 생략)"
        try:
            asks = ai_client.clarify(req.question, answered, clar_ctx)
        except ai_client.AiError as e:
            applog.warn(f"되묻기 판단 실패 — 건너뜁니다: {e}")
            asks = []

        if asks and it.kind == intent_mod.DEADLINE:
            _has_date = bool(re.search(r"\d{4}\s*[년.\-/]|\d+\s*(년|개월|달)\s*(전|째|됐|되었|지났|경과)|다음\s*\S{0,6}(언제|검사|점검)",
                                       req.question + " " + (answered or "")))
            if not _has_date:
                _dq = [a for a in asks if re.search(r"(날짜|언제입니까|언제였|며칠입니까|일자)", a["question"])
                       or any("날짜 입력" in str(o) for o in a.get("options", []))]
                if _dq:
                    asks = [a for a in asks if a not in _dq]
                    steps.append({"name": "날짜 되묻기 생략",
                                  "detail": "법정 기한을 묻는 질문이라 사건 날짜는 묻지 않음: "
                                            + " / ".join(a["question"][:30] for a in _dq)})
        if asks:
            already = _asked_questions(answered)
            fresh = [a for a in asks if not _is_repeat_question(a["question"], already)]
            if not fresh:
                applog.debug("clarify", f"이미 물은 질문 {len(asks)}개 반복 감지 → 되묻기 종료")
                steps.append({"name": "되묻기 종료",
                              "detail": "같은 질문이 반복돼 다음 단계로 진행"})
            asks = fresh

        if not asks:
            steps.append({"name": "되묻기 판단",
                          "detail": f"더 물을 것 없음 (조문 {len(parts)}개 본문 기준)"})
        if asks:
            steps.append({"name": "질문 확인",
                          "detail": f"{req.round + 1}차 · {len(asks)}개 항목 "
                                    f"(조문 {len(parts)}개 본문 기준)"})
            return {
                "clarify": asks,
                "round": req.round + 1,
                "max_rounds": max_r,
                "answered": answered,
                "steps": steps,
                "intent": it.as_dict(),
            }

    def _assemble(sel):
        """법령별로 묶어 컨텍스트 텍스트를 만듭니다 (법령 순서는 use 에 처음 나온 순서)."""
        by_law = {}
        for f, a, txt in sel:
            by_law.setdefault(id(f), (f, []))[1].append(txt)
        return "\n\n".join(
            f"=== 여기부터는 「{f['name']}」 조문입니다 "
            f"(시행 {f.get('enforced', '?')}) ===\n" + "\n\n".join(txts)
            for f, txts in by_law.values()), len(by_law)

    _hint = {
        intent_mod.DEFINITION: "\n(질문 유형: 용어 정의 — 정의 조문을 먼저 인용하고 쉽게 풀어 설명한다)",
        intent_mod.GENERAL_RULE: "\n(질문 유형: 일반 기준·절차 설명 — 특정 사례를 판정하는 질문이 아니다. "
                                 "조건에 따라 기준이 달라지면 경우를 나눠 정리한다)",
    }.get(it.kind, "")
    _neg = _negated_targets(req.question)
    if _neg:
        _hint += ("\n(질문자 확인: " + ", ".join(f"「{n}」에 해당하지 않음" for n in _neg)
                  + " — 그 대상에만 적용되는 조문은 이 사안에 적용되지 않는다. 누구에게나 적용되는 일반 조문"
                    "(예: 신고·조사·명령의 일반 규정)이 직접 근거이므로 그 조문으로 답한다. "
                    "'직접적인 규정을 확인할 수 없다' 고 하지 않는다)")
        steps.append({"name": "질문자가 아니라고 밝힌 대상", "detail": ", ".join(_neg) + " — 일반 조문으로 답하도록 지시"})
    _asp = next((f.get("_aspects") for f in found if f.get("_aspects")), None)
    if _asp:
        _hint += ("\n(이 질문은 여러 측면을 함께 묻는다: " + " / ".join(f"{i + 1}) {q}" for i, q in enumerate(_asp))
                  + " — 【결론】과 【설명】에서 측면마다 근거 조문을 들어 나눠 답한다. 조문이 없는 측면은 그렇다고 밝힌다)")
    try:
        _hint += _history_block(req.question, use, prio, bp_kw, steps, answered or "")
    except Exception as e:                                # noqa: BLE001
        applog.warn(f"개정 이력 비교 실패 — 건너뜁니다: {e}")
    _pre_cases = None
    if RELATED_CASES and CASE_IN_ANSWER:
        try:
            _tw0 = next((s_.get("detail", {}).get("용어", []) for s_ in steps
                         if s_.get("name") == "검색어 변환" and isinstance(s_.get("detail"), dict)), [])
            _pre_cases = _case_finalize(req.question + (f"\n(확인된 조건: {answered})" if answered else ""),
                                        _case_candidates(req.question, _tw0, found))
            _ci = _pre_cases.get("items") or []
            if _ci:
                _keep = ai_client.confirm_cases(req.question, _ci)
                if _keep is None:
                    steps.append({"name": "참고 해석례 확인", "detail": "본문 대조 실패 — 답변에는 넣지 않고 화면에만 표시"})
                    _ci = []
                else:
                    _n0 = len(_ci)
                    _ci = [_ci[k] for k in _keep]
                    _pre_cases = {"items": _ci, "detail": (_pre_cases.get("detail", "") +
                                                           f" → 본문 대조 {_n0}건 중 {len(_ci)}건")}
                    steps.append({"name": "참고 해석례 확인", "detail": f"본문 대조 — {_n0}건 중 {len(_ci)}건 같은 쟁점"})
            if _ci:
                _hint += ("\n\n[참고 해석례 — 서버가 법제처에서 가져온 실제 법령해석·부처 해석·판례다. "
                          "근거 조문은 [조문 원문]에서 든다. 해석은 그 날짜 당시 조문 기준이라 그 뒤 개정됐을 수 있다. "
                          "해석이 근거로 든 조문 문구가 지금 [조문 원문]과 다르면(대상·요건·'…내에서' 같은 제한이 바뀜) 현행 조문 문언대로 결론 내고 "
                          "\"(참고: ○○ 해석은 개정 전 조문 기준)\" 이라고 밝힌다. 문구가 같고 같은 쟁점이면 그 해석 결론과 어긋나게 답하지 말고 "
                          "\"(참고: 기관 해석 날짜)\" 처럼 밝힌다. 질문과 사실관계가 다르면 쓰지 않는다]\n"
                          + "\n".join(f"{k + 1}) [{c['kind']}] {c['title'][:90]} ({c.get('org', '')} {c.get('date', '')})\n"
                                      f"   질의: {c['q'][:220]}\n   회답: {c['a'][:480]}" for k, c in enumerate(_ci[:2])))
        except Exception as e:                            # noqa: BLE001
            applog.warn(f"참고 해석례(답변 전) 실패 — 건너뜁니다: {e}")
            _pre_cases = None
    q_for_answer = req.question + (f"\n(확인된 조건: {answered})" if answered else "") + _hint
    ctx_n = ai_client.server_ctx()
    overhead = ai_client.count_tokens(
        ai_client.ANSWER_PROMPT.format(question=q_for_answer, context=""))
    budget = min(CTX_BUDGET_MAX, ctx_n - overhead - min(ai_client.MAXTOK_ANSWER, ANSWER_RESERVE) - 96)
    if budget < MIN_CTX_BUDGET and _hint:
        _ht = ai_client.count_tokens(_hint) or 1
        _keep_r = max(0.0, (_ht - (MIN_CTX_BUDGET - budget) * 1.1) / _ht)
        _hint = _hint[:int(len(_hint) * _keep_r)] + ("\n…(참고 블록 분량 제한으로 생략)" if _keep_r < 1 else "")
        q_for_answer = req.question + (f"\n(확인된 조건: {answered})" if answered else "") + _hint
        overhead = ai_client.count_tokens(ai_client.ANSWER_PROMPT.format(question=q_for_answer, context=""))
        budget = min(CTX_BUDGET_MAX, ctx_n - overhead - min(ai_client.MAXTOK_ANSWER, ANSWER_RESERVE) - 96)
        steps.append({"name": "참고 블록 축소", "detail": f"조문 예산 확보 — 덧붙임 {_ht:,}토큰 중 {int(_keep_r * 100)}% 남김"})

    keep = list(units)
    _cut_keys = None
    context, n_laws = _assemble(keep)
    n_tok = ai_client.count_tokens(context)
    dropped = []
    if n_tok > budget:
        shrunk = 0
        for j, u in enumerate(keep):
            if _is_bp(u) and len(u[2]) > BP_SHRINK_CHARS:
                keep[j] = (u[0], u[1], _fit_keep_notes(u[2], BP_SHRINK_CHARS, bp_kw or None))
                shrunk += 1
        if shrunk:
            before_tok = n_tok
            context, n_laws = _assemble(keep)
            n_tok = ai_client.count_tokens(context)
            steps.append({"name": "별표 축약",
                          "detail": f"예산 {budget:,}토큰 초과 — 별표 {shrunk}개를 1,800자로 줄임(비고 유지) "
                                    f"{before_tok:,} → {n_tok:,}토큰"})
    if n_tok > budget:
        pos = {id(u[1]): i for i, u in enumerate(keep)}
        order = sorted(keep, key=lambda u: (-prio.get(id(u[1]), float(len(keep))), -pos[id(u[1])]))
        for _ in range(5):
            ratio = n_tok / max(1, len(context))
            need = (n_tok - budget) * 1.1
            while need > 0 and order and len(keep) > 3:
                u = order.pop(0)
                keep.remove(u)
                dropped.append(u)
                need -= len(u[2]) * ratio
            context, n_laws = _assemble(keep)
            n_tok = ai_client.count_tokens(context)
            if n_tok <= budget or len(keep) <= 3 or not order:
                break
        if dropped and n_tok <= budget and ctx_guard.enabled("refill"):
            try:
                keep, dropped, context, n_laws, n_tok, _back = ctx_guard.refill(
                    keep, dropped, prio, pos, _assemble, ai_client.count_tokens, budget - 400)
                if _back:
                    steps.append({"name": "조문 되돌려 넣기",
                                  "detail": f"남는 예산으로 {len(_back)}개 다시 넣음: "
                                            + ", ".join(f"「{f['name']}」 {label_of(a)}" for f, a, _ in _back[:5])
                                            + f" → {n_tok:,}토큰"})
            except Exception as e:                        # noqa: BLE001
                applog.warn(f"조문 되돌려 넣기 실패 — 건너뜁니다: {e}")
        if n_tok > budget:
            cut = max(0, int(len(context) * max(budget, 0) / n_tok * 0.95))
            context = context[:cut] + "\n…(분량 제한으로 이하 생략)"
            n_tok = ai_client.count_tokens(context)
            _cut_keys = [_ctx_key(f, a) for f, a, t in keep if str(t)[:80] in context]
        if dropped:
            names = [f"「{f['name']}」 {label_of(a)}" for f, a, _ in dropped]
            context += ("\n\n(※ 분량 제한으로 다음 조문은 넣지 못했습니다: " + ", ".join(names[:15])
                        + (" 외" if len(names) > 15 else "")
                        + ". 이 조문의 내용이 필요하면 추측하지 말고 원문 확인이 필요하다고 답하십시오.)")
            steps.append({"name": "조문 분량 조절",
                          "detail": f"예산 {budget:,}토큰 초과 — {len(dropped)}개 제외: "
                                    + ", ".join(names[:5]) + (" 외" if len(names) > 5 else "")})
    steps.append({
        "name": "조문 수집",
        "detail": f"{n_laws}개 법령 / 조문 {len(keep)}개 / {n_tok:,}토큰 "
                  f"(예산 {budget:,}, 컨텍스트 {ctx_n:,})",
    })
    if not context.strip():
        return {"steps": steps, "answer": "조문 본문을 가져오지 못했습니다.", "laws": found}
    if n_tok >= QUESTION_REPEAT_TOKENS:
        context += "\n\n(조문 원문 끝. 위 조문으로 답할 질문을 다시 적는다: " + re.sub(r"\s+", " ", req.question.strip())[:400] + ")"

    _cases_fut = None
    if RELATED_CASES and _pre_cases is None:
        try:
            from concurrent.futures import ThreadPoolExecutor
            _cases_ex = ThreadPoolExecutor(max_workers=1)
            _tw = next((s_.get("detail", {}).get("용어", []) for s_ in steps
                        if s_.get("name") == "검색어 변환" and isinstance(s_.get("detail"), dict)), [])
            _cases_fut = _cases_ex.submit(_case_candidates, req.question, _tw, found)
            _cases_ex.shutdown(wait=False)
        except Exception as e:                            # noqa: BLE001
            applog.warn(f"참고 사례 검색 시작 실패 — 건너뜁니다: {e}")

    try:
        text = ai_client.answer(q_for_answer, context)
    except ai_client.AiError as e:
        return _err(str(e))
    _trunc = applog.last_finish("answer") == "length"
    if _neg and "【결론】" in text:
        pre = text[:text.index("【결론】")]
        if pre.strip() and re.search(r"제공된\s*(\[?조문|원문)", pre):
            text = text[text.index("【결론】"):]

    if ANSWER_REVIEW and len(text) > 80:
        try:
            probs = ai_client.review_answer(q_for_answer, text, context)
            _sc = _SELF_CORR_RE.search(text)
            if _sc and not any(p.get("kind") == "결론모순" for p in probs):
                probs = list(probs) + [{"kind": "결론모순", "quote": text[_sc.start():_sc.start() + 80].strip(),
                                        "fix": _SELF_CORR_FIX}]
            _vc = _verdict_conflict(text)
            if _vc and not any(p.get("kind") == "결론모순" for p in probs):
                probs = list(probs) + [{"kind": "결론모순", "quote": _vc[:80],
                                        "fix": "【결론】의 판정이 【계산】의 판정과 반대다. 【계산】을 조문 원문으로 다시 확인해 "
                                               "하나의 판정으로 맞추고 【결론】부터 다시 쓴다"}]
            if _trunc:
                probs = list(probs) + [{"kind": "잘림", "quote": text.strip()[-80:],
                                        "fix": "답이 출력 한도에 걸려 끝까지 쓰이지 않았다. 같은 내용 되풀이·고친 흔적 없이 "
                                               "필요한 블록만 간결하게 처음부터 다시 쓴다"}]
            if probs:
                text2 = ai_client.answer_revised(q_for_answer, context, probs)
                _trunc2 = applog.last_finish("answer") == "length"
                if _neg and "【결론】" in text2:
                    pre2 = text2[:text2.index("【결론】")]
                    if pre2.strip() and re.search(r"제공된\s*(\[?조문|원문)", pre2):
                        text2 = text2[text2.index("【결론】"):]
                _c1 = sum(1 for c in verify_citations(text, found) if c["ok"])
                _c2 = sum(1 for c in verify_citations(text2, found) if c["ok"])
                _keep_secs = all(h in text2 for h in ("【결론】", "【근거】", "【계산】", "【설명】") if h in text)
                if "【결론】" in text2 and len(text2) > 80 and "【근거】" in text2 and _c2 >= max(1, _c1 // 2) and _keep_secs:
                    text, _trunc = text2, _trunc2
                elif any(p.get("kind") == "결론모순" for p in probs) and _merge_conclusion(text, text2):
                    text = _merge_conclusion(text, text2)
                    probs = [dict(p, kind=p["kind"] + "(결론만 반영)") for p in probs]
                else:
                    probs = [dict(p, kind=p["kind"] + "(다시 쓴 답 버림)") for p in probs]
                steps.append({"name": "답변 검토",
                              "detail": f"오류 {len(probs)}건 → 고쳐 다시 씀: "
                                        + " / ".join(f"[{p['kind']}] {p['fix'][:60]}" for p in probs)})
            elif not ai_client.REVIEW_STATS.get("ok", True):
                steps.append({"name": "답변 검토", "detail": "검토 실패(응답 오류) — 처음 답변을 그대로 씁니다"})
            else:
                _rs = ai_client.REVIEW_STATS
                steps.append({"name": "답변 검토",
                              "detail": f"문제 없음 (핵심 주장 {_rs.get('claims', 0)}개 대조"
                                        + (f", 원문 근거를 못 찾은 지적 {_rs['dropped']}건 버림" if _rs.get("dropped") else "")
                                        + ")"})
        except Exception as e:                            # noqa: BLE001
            applog.warn(f"답변 검토 실패 — 처음 답변을 씁니다: {type(e).__name__}: {e}")

    warnings = []
    _vc_final = _verdict_conflict(text)
    if _vc_final and CONCL_FIX:
        _t3 = _merge_conclusion(text, ai_client.fix_conclusion(
            req.question + (f"\n(확인된 조건: {answered})" if answered else ""), _CONCL_BLOCK_RE.sub("", text, 1).strip()))
        if _t3 and not _verdict_conflict(_t3):
            text, _vc_final = _t3, ""
            warnings.append("처음 쓴 【결론】이 【계산】·【설명】의 판정과 달라, 그 판정에 맞춰 【결론】만 다시 썼습니다. "
                            "수치는 근거 조문 원문으로 확인하세요.")
            steps.append({"name": "결론 다시 씀", "detail": "【결론】이 계산·설명의 판정과 반대 → 본문 판정에 맞춰 결론만 다시 씀"})
    if _vc_final:
        warnings.append("답변의 【결론】과 계산·설명의 판정이 서로 다릅니다. 근거 조문 원문과 수치로 직접 확인하세요.")
        steps.append({"name": "결론 불일치 경고", "detail": _vc_final[:60]})

    cites = verify_citations(text, found)
    steps.append({"name": "인용 검증", "detail": f"{sum(1 for c in cites if c['ok'])}/{len(cites)}건 확인"})

    if _trunc:
        warnings.append("답변이 길이 한도에서 잘렸습니다. 뒷부분이 빠졌을 수 있으니 근거 조문 원문으로 확인하세요.")
        steps.append({"name": "답변 잘림", "detail": "출력 상한에 걸려 끝까지 쓰이지 않음 — 화면에 경고 표시"})
    if missing_bp:
        cited_bp = _bp_used_as_basis(text)
        hits = [m for m in missing_bp
                if any(k == m["kind"] and n == m["no"] and (not lw or _law_key(lw) == _law_key(m["law"]))
                       for k, n, lw in cited_bp)]
        if hits:
            names = ", ".join(
                f"「{m['law']}」 {m['kind']} {m['no']}"
                + (f"({m['title']})" if m.get("title") else "")
                for m in hits)
            warnings.append(
                f"답변이 {names} 을(를) 근거로 들었으나, 이 별표의 본문은 법제처가 "
                f"API 로 제공하지 않아 가져오지 못했습니다. "
                f"답변에 적힌 주기·기간·수치는 확인된 근거가 없으므로 그대로 사용하지 마시고 "
                f"아래 주소에서 원문을 직접 확인하세요."
            )
            for m in hits:
                if m.get("link"):
                    warnings.append(f"{m['kind']} {m['no']} 원문 보기: {m['link']}")
            steps.append({"name": "별표 경고", "detail": names + " — 근거 없는 수치 경고 표시"})

    try:
        calc_warn = verify_calc(text, f"{req.question}\n{answered}")
    except Exception as e:                                # noqa: BLE001
        applog.warn(f"계산 검산 실패 — 건너뜁니다: {type(e).__name__}: {e}")
        calc_warn = []
    if calc_warn:
        warnings.extend(calc_warn)
        steps.append({"name": "계산 검산", "detail": f"{len(calc_warn)}건 이상 발견"})

    hit = sorted({(c["law"], c["jo"], c["gaji"]) for c in cites if c["ok"]})

    cases = []
    if _pre_cases is not None:
        cases = _pre_cases.get("items") or []
        steps.append({"name": "참고 사례", "detail": _pre_cases.get("detail", "") + " · 답변에 참고로 넣음"
                      if cases else _pre_cases.get("detail", "")})
    elif _cases_fut is not None:
        try:
            rc = _case_finalize(req.question, _cases_fut.result(timeout=20))
            cases = rc.get("items") or []
            steps.append({"name": "참고 사례", "detail": rc.get("detail", "")})
        except Exception as e:                            # noqa: BLE001
            applog.warn(f"참고 사례 검색 실패 — 건너뜁니다: {e}")

    return {
        "steps": steps, "answer": text, "laws": _laws_by_cites(found, cites), "citations": cites,
        "cases": cases,
        "cited_keys": [f"{n}|{j}|{g}" for n, j, g in hit],
        "references": [r for r, _ in sorted(refs.items(), key=lambda x: -x[1])][:12],
        "warnings": warnings,
        "intent": it.as_dict(),
        "debug": {
            "flat_n": len(flat),
            "picked_n": len(picked or []),
            "context_keys": _cut_keys if _cut_keys is not None else [_ctx_key(f, a) for f, a, _ in keep],
            "dropped_keys": [_ctx_key(f, a) for f, a, _ in dropped],
            "user_laws": _user_laws(req),
            "laws_mode": _laws_mode(req),
            "ai_hits": [f"{n} 제{str(j).lstrip('0')}조" + (f"의{str(g).lstrip('0')}" if str(g).strip("0") else "")
                        for n, j, g in (ai_hits or [])[:20]],
        },
    }
