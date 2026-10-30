# -*- coding: utf-8 -*-
"""
★ v1.49 (2026-10-30) — llama-server 프롬프트 캐시(KV)를 실제로 다시 쓰게 하기

왜 필요한가 (10-30 llama-server 로그 1,756회 분석)
    · LLM 시간의 53%가 프롬프트 읽기(prefill, 약 950 tok/s)인데, 976만 토큰 중 재사용은 1%뿐이었습니다.
    · Qwen3.6-35B-A3B 는 40층 중 30층이 Gated DeltaNet(되감을 수 없는 순환 상태)이라,
      **새 프롬프트가 캐시에 남은 내용 전체를 그대로 앞에 포함할 때만** 재사용됩니다.
      우리 호출은 단계마다 지시문이 달라 거의 해당이 없었습니다.

이 파일이 하는 일 (ai_client.py 는 부르기만 합니다)
    ① 이어 쓰기 — 답변 호출을 /completion 으로 보내 생성 토큰을 받아 두고, 답변 검토는
       [답변 프롬프트 + 생성된 답변 토큰 + 검토 지시] 로 **이어서** 보냅니다.
       조문·질문·답변(약 8~10k 토큰)을 다시 읽지 않습니다.
    ② 고정 앞부분 상태 저장·복원 — 답변·되묻기 지시문처럼 매번 똑같은 앞부분(답변은 약 3k 토큰)을
       한 번 처리한 상태를 파일로 저장(/slots save)해 두고, 다음 호출 전에 복원(/slots restore)합니다.
       llama-server 를 --slot-save-path 로 띄워야 동작합니다(아니면 저절로 건너뜀).

안전장치
    · 무엇이든 실패하면 None 을 돌려주고, 호출하는 쪽이 예전 방식(/chat/completions)으로 다시 부릅니다.
    · 복원과 호출 사이에 다른 요청이 끼면 상태가 바뀌므로, 모든 LLM 호출이 LOCK 을 함께 씁니다
      (llama-server 가 -np 1 이라 원래 한 번에 하나만 처리 — 처리량은 같음).

끄기: _cache\\flags\\kv_off 파일 만들기 (또는 .env KV_REUSE=0). ②만 끄기: _cache\\flags\\kv_prefix_off.
되돌리기: ai_client.py 의 '★ v1.49 kv_reuse' 블록 삭제 후 이 파일 삭제.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time

import httpx

import applog

HERE = os.path.dirname(os.path.abspath(__file__))
FLAG_DIR = os.path.join(HERE, "_cache", "flags")

LOCK = threading.RLock()                 # ai_client._call_local 도 이 잠금을 씁니다
PREFIX_STAGES = ("answer", "clarify")    # 고정 앞부분이 큰 단계(답변 지시문 약 8천 자, 되묻기 약 2천 자)
MIN_PREFIX_CHARS = 1500
CHAIN_TTL = 900.0

_STATE = {"tpl": None, "tpl_fail_t": 0.0, "eog": None, "slots_ok": None, "slots_fail_t": 0.0}
_PFX: dict = {}                          # stage → {"last", "text", "ids", "file"}
_TL = threading.local()                  # 요청 스레드별 '방금 쓴 답변' 이어 쓰기 정보


def _chain_enabled() -> bool:
    """
    ★ v1.49a — ① 이어 쓰기(답변 검토)는 기본으로 끕니다. 켜기: _cache\\flags\\kv_chain_on 파일(또는 .env KV_CHAIN=1).
    10-30 평가: 모델이 **방금 자기가 쓴 답을 같은 대화 안에서** 검토하자 '문제 없음'이 크게 늘어(다시 쓰기 9→1회,
    핵심 주장 5.1→3.6개) 해석 문항이 15/19 → 12/19 로 떨어졌습니다. 검토 시간은 24초 → 11초로 줄었지만 품질 우선.
    """
    if os.getenv("KV_CHAIN") == "1":
        return True
    return os.path.exists(os.path.join(FLAG_DIR, "kv_chain_on"))


def enabled() -> bool:
    if os.getenv("KV_REUSE", "1") == "0":
        return False
    return not os.path.exists(os.path.join(FLAG_DIR, "kv_off"))


def _prefix_enabled() -> bool:
    if os.path.exists(os.path.join(FLAG_DIR, "kv_prefix_off")):
        return False
    if _STATE["slots_ok"] is False and time.time() - _STATE["slots_fail_t"] < 3600:
        return False
    return True


def _root() -> str:
    import ai_client
    return ai_client._server_root()


def template():
    """(head, tail, eot) — 사용자 메시지 하나를 서버 채팅 템플릿으로 감쌀 때의 앞·뒤 글자. 실패하면 None."""
    if _STATE["tpl"]:
        return _STATE["tpl"]
    if time.time() - _STATE["tpl_fail_t"] < 600:
        return None
    s = "␟KVSENTINEL␟"
    try:
        r = httpx.post(f"{_root()}/apply-template",
                       json={"messages": [{"role": "user", "content": s}],
                             "chat_template_kwargs": {"enable_thinking": False}}, timeout=10)
        r.raise_for_status()
        p = str(r.json().get("prompt") or "")
        i = p.find(s)
        if i < 0:
            raise ValueError("템플릿에서 메시지 위치를 못 찾음")
        head, tail = p[:i], p[i + len(s):]
        if "<|im_start|>" not in tail or "<|im_start|>" not in head:
            raise ValueError("Qwen 대화 표시(<|im_start|>)가 없음")
        eot = tail[:tail.index("<|im_start|>")]
        if not eot.strip():
            raise ValueError("턴 끝 표시가 없음")
        _STATE["tpl"] = (head, tail, eot)
        return _STATE["tpl"]
    except Exception as e:                               # noqa: BLE001
        _STATE["tpl_fail_t"] = time.time()
        applog.warn(f"KV 재사용 건너뜀(채팅 템플릿 확인 실패, 10분 뒤 다시): {type(e).__name__}: {e}")
        return None


def _tokenize(text: str) -> list:
    r = httpx.post(f"{_root()}/tokenize",
                   json={"content": text, "add_special": False, "parse_special": True}, timeout=30)
    r.raise_for_status()
    out = []
    for t in r.json().get("tokens") or []:
        out.append(t if isinstance(t, int) else int(t.get("id")))
    return out


def _eog_id():
    if _STATE["eog"] is None:
        try:
            ids = _tokenize("<|im_end|>")
            _STATE["eog"] = ids[0] if len(ids) == 1 else -1
        except Exception:                                # noqa: BLE001
            _STATE["eog"] = -1
    return _STATE["eog"]


def _as_prompt(parts: list):
    """문자열만 여러 개면 서버가 '여러 프롬프트' 로 보므로, 숫자가 없으면 하나로 합칩니다."""
    if not any(isinstance(p, int) for p in parts):
        return "".join(parts)
    return parts


def complete(parts: list, temperature: float, max_tokens: int, stage: str, schema=None,
             return_tokens: bool = False, slot: int | None = None):
    """/completion 한 번. (본문, 생성 토큰 id, 재사용 토큰 수). 실패하면 예외."""
    import ai_client
    body = {"prompt": _as_prompt(parts), "temperature": temperature, "n_predict": int(max_tokens),
            "cache_prompt": True, "return_tokens": bool(return_tokens)}
    if slot is not None:
        body["id_slot"] = slot
    if schema is not None and ai_client.json_enabled():
        body["json_schema"] = schema
    t0 = time.time()
    r = httpx.post(f"{_root()}/completion", json=body, timeout=ai_client.LOCAL_TIMEOUT)
    r.raise_for_status()
    d = r.json()
    tm = d.get("timings") or {}
    cache_n = int(tm.get("cache_n") or 0)
    prompt_n = int(tm.get("prompt_n") or d.get("tokens_evaluated") or 0)
    pred = int(tm.get("predicted_n") or d.get("tokens_predicted") or 0)
    finish = "length" if (d.get("stop_type") == "limit" or d.get("truncated")) else "stop"
    ai_client._llm_health(True)
    applog.llm(stage, prompt_n + cache_n, pred, time.time() - t0, finish=finish,
               tps=tm.get("predicted_per_second"),
               note=(f"KV 재사용 {cache_n:,}토큰 · 새로 읽음 {prompt_n:,}" if cache_n else f"KV 재사용 없음 · {prompt_n:,}"))
    return str(d.get("content") or ""), list(d.get("tokens") or []), cache_n


# ── ② 고정 앞부분 상태 저장·복원 ─────────────────────────────────
def _prefix_parts(stage: str, full: str):
    """
    full(템플릿까지 씌운 프롬프트)의 고정 앞부분을 복원해 쓸 수 있으면 [앞부분 토큰들…, 나머지 글자] 와
    복원 여부를 돌려줍니다. 앞부분은 같은 단계의 직전 프롬프트와 겹치는 부분(줄 단위)으로 스스로 찾습니다.
    잠금(LOCK) 안에서 불러야 합니다.
    """
    if stage not in PREFIX_STAGES or not _prefix_enabled():
        return [full], False
    st = _PFX.setdefault(stage, {"last": "", "text": "", "ids": None, "file": ""})
    last, st["last"] = st["last"], full
    if not (st["text"] and full.startswith(st["text"])):
        if not last:
            return [full], False
        cp = os.path.commonprefix([last, full])
        cuts = [m.end() for m in re.finditer(r"\n(?=\S)", cp)]
        k = cuts[-1] if cuts else 0
        if k < MIN_PREFIX_CHARS:
            return [full], False
        st.update(text=full[:k], ids=None, file="")
    try:
        if st["ids"] is None:
            st["ids"] = _tokenize(st["text"])
        name = f"pfx_{stage}_{hashlib.sha1(st['text'].encode('utf-8')).hexdigest()[:12]}.bin"
        if st["file"] != name:
            # 처음: 앞부분만 처리해 저장(이 처리는 이번 호출이 어차피 해야 할 일 — 바로 이어서 씀)
            complete(list(st["ids"]), 0.0, 0, stage + "_pfx", slot=0)
            r = httpx.post(f"{_root()}/slots/0?action=save", json={"filename": name}, timeout=60)
            if r.status_code >= 400:
                raise RuntimeError(f"slots save HTTP {r.status_code}: {r.text[:120]}")
            st["file"] = name
            _STATE["slots_ok"] = True
        else:
            r = httpx.post(f"{_root()}/slots/0?action=restore", json={"filename": name}, timeout=60)
            if r.status_code >= 400:
                st["file"] = ""
                raise RuntimeError(f"slots restore HTTP {r.status_code}: {r.text[:120]}")
        return list(st["ids"]) + [full[len(st["text"]):]], True
    except Exception as e:                               # noqa: BLE001
        _STATE["slots_ok"], _STATE["slots_fail_t"] = False, time.time()
        applog.warn(f"KV 앞부분 저장·복원 건너뜀(1시간 뒤 다시 — llama-server 를 --slot-save-path 로 띄웠는지 확인): "
                    f"{type(e).__name__}: {e}")
        return [full], False


def prefix_call(prompt: str, temperature: float, max_tokens: int, stage: str, schema=None):
    """되묻기 등 일반 호출에 ② 를 적용합니다. 쓸 수 없으면 None(→ 예전 방식)."""
    if stage not in PREFIX_STAGES or not enabled() or not _prefix_enabled():
        return None
    tpl = template()
    if not tpl:
        return None
    head, tail, _ = tpl
    with LOCK:
        parts, restored = _prefix_parts(stage, head + prompt + tail)
        if not restored:
            return None                                  # 저장된 앞부분이 없으면 예전 방식 그대로
        try:
            text, _, _ = complete(parts, temperature, max_tokens, stage, schema=schema, slot=0)
            return text
        except Exception as e:                           # noqa: BLE001
            applog.warn(f"KV 호출 실패 — 예전 방식으로: {type(e).__name__}: {e}")
            return None


# ── ① 답변 → 검토 이어 쓰기 ────────────────────────────────────
def answer_call(prompt: str, temperature: float, max_tokens: int, stage: str = "answer"):
    """답변을 부르고 이어 쓰기 정보를 기억합니다. 실패하면 None(→ 예전 방식)."""
    if not enabled():
        return None
    tpl = template()
    if not tpl:
        return None
    head, tail, _ = tpl
    _TL.chain = None
    with LOCK:
        parts, restored = _prefix_parts(stage, head + prompt + tail)
        try:
            text, toks, _ = complete(parts, temperature, max_tokens, stage, return_tokens=True,
                                     slot=0 if restored else None)
        except Exception as e:                           # noqa: BLE001
            applog.warn(f"KV 답변 호출 실패 — 예전 방식으로: {type(e).__name__}: {e}")
            return None
    eog = _eog_id()
    toks = [int(t) for t in toks if isinstance(t, (int, float))]
    if toks and toks[-1] == eog:
        toks = toks[:-1]
    if toks:
        _TL.chain = {"parts": parts, "gen": toks, "text": text, "t": time.time()}
    return text


def _qn(t: str) -> str:
    return re.sub(r"[^가-힣A-Za-z0-9]+", "", str(t or ""))


def chain_call(answer_text: str, user2: str, temperature: float, max_tokens: int, stage: str, schema=None):
    """
    방금 쓴 답변에 이어서 user2 를 보냅니다(답변 프롬프트·답변을 다시 읽지 않음).
    answer_text 가 기억해 둔 답변과 다르거나(다시 쓴 답 등) 쓸 수 없으면 None(→ 예전 방식).
    """
    ch = getattr(_TL, "chain", None)
    if not _chain_enabled():                 # ★ v1.49a — 기본 꺼짐(위 _chain_enabled 설명)
        return None
    if not ch or not enabled() or time.time() - ch["t"] > CHAIN_TTL:
        return None
    a = _qn(answer_text)[:200]
    if not a or a not in _qn(ch["text"]):
        return None
    tpl = template()
    if not tpl:
        return None
    head, tail, eot = tpl
    user_hdr = head[head.rfind("<|im_start|>"):]
    gen_hdr = tail[len(eot):]
    suffix = eot + user_hdr + user2 + gen_hdr
    import ai_client
    try:
        n_suffix = ai_client.count_tokens(suffix) + 16
        n_prev = sum(1 for p in ch["parts"] if isinstance(p, int)) + \
            sum(ai_client.count_tokens(p) for p in ch["parts"] if isinstance(p, str)) + len(ch["gen"])
        room = ai_client.server_ctx() - n_prev - n_suffix - 16
        if room < 200:
            return None
        with LOCK:
            text, _, _ = complete(list(ch["parts"]) + list(ch["gen"]) + [suffix], temperature,
                                  min(int(max_tokens), room), stage, schema=schema)
        return text
    except Exception as e:                               # noqa: BLE001
        applog.warn(f"KV 이어 쓰기 실패 — 예전 방식으로: {type(e).__name__}: {e}")
        return None


# ── ★ v1.49a — llama-server 한 번 다시 띄우기(실행 옵션 반영용) ─────────────────
# _cache\\flags\\llm_restart_once 파일이 있으면 서버(이 프로그램)가 그 파일을 지우고, 5초 뒤
# ai_client.restart_local_llm()(워치독이 쓰는 것과 같은 재시작: LLM 창 정리 → _5_restart_llm.bat)을 한 번 부릅니다.
# start.bat·_5_restart_llm.bat 의 --slot-save-path 같은 새 옵션을 사람이 창을 닫지 않고 반영하려고 넣었습니다.
# 파일을 먼저 지우므로 한 번만 동작합니다. 되돌리기: 아래 함수와 호출 삭제.
def _restart_once() -> None:
    p = os.path.join(FLAG_DIR, "llm_restart_once")
    if not os.path.exists(p):
        return
    try:
        os.remove(p)
    except OSError:
        return

    def _run():
        time.sleep(5)
        try:
            import ai_client
            ai_client.restart_local_llm("요청 파일(_cache/flags/llm_restart_once) — 바뀐 실행 옵션 반영")
        except Exception as e:                           # noqa: BLE001
            applog.warn(f"LLM 한 번 다시 띄우기 실패: {type(e).__name__}: {e}")
    threading.Thread(target=_run, daemon=True).start()


_restart_once()
