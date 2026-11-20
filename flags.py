# -*- coding: utf-8 -*-
"""켜고 끄는 스위치 — `_cache/flags/` 의 빈 파일, 또는 같은 뜻의 환경변수(.env). 표준 라이브러리만."""
import os

FLAG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_cache", "flags")

KNOWN = (
    "answer_cache", "clarify_why", "law_cache",
    "kv_off", "kv_prefix_off", "kv_chain_on", "llm_restart_once",
)


def path(name: str) -> str:
    return os.path.join(FLAG_DIR, name)


def has(name: str) -> bool:
    """플래그 파일이 있는지."""
    return os.path.exists(path(name))


def on(name: str, env: str) -> bool:
    """환경변수 env 가 비어 있지 않으면 그 값('1' = 켬), 없으면 플래그 파일이 있을 때 켬."""
    v = os.getenv(env)
    if v is not None and v.strip() != "":
        return v.strip() == "1"
    return has(name)
