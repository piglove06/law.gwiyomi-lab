"""
v1.41 — 법령 본문 로컬 캐시 (법령 버전 = 법령일련번호(MST) 기준).

왜 필요한가
  지금은 질문마다 법제처에서 법령 본문(수백 조문)을 다시 받고 10분만 메모리에 둡니다.
  법령은 개정될 때만 바뀌므로, 버전(MST)이 같으면 저장해 둔 본문을 그대로 써도 됩니다.
  → 법제처 대기 시간이 줄고(속도), 이후 본문 유사도 색인(rag_select)의 바탕이 됩니다.

어떻게
  - 열쇠: (조회 대상 target, 법령 ID, MST, 시행일자). 법제처 **검색 결과에 이미 들어 있는** 현행 MST 로
    맞춰 보므로 '버전 확인용' 호출이 따로 없습니다. 개정되면 MST 가 바뀌어 자동으로 새로 받습니다.
  - 위치: _cache/laws/<target>/<법령ID>/<MST>_<시행일자>.json  (_cache 는 .gitignore — 공개 저장소에 안 올라감)
  - 법령마다 최근 3개 버전만 남깁니다(개정 전후 비교 등에 쓸 여지).
  - 조문을 제대로 못 받은 응답(조문 0개)은 저장하지 않습니다.
  - 파일이 깨졌으면 지우고 새로 받습니다.

켜고 끄기 (기본 꺼짐)
  - 환경변수 LAW_CACHE=1 켜짐 / =0 꺼짐(파일보다 우선)
  - 환경변수가 없으면 플래그 파일 _cache/flags/law_cache 가 있을 때만 켜짐(재시작 없이 바로 반영)

원복
  main.py 의 '★ v1.41 캐시' 한 줄을 원래 law_client.get_detail 호출로 되돌리고 이 파일을 지우면 됩니다.
"""
from __future__ import annotations

import glob
import json
import os
import re
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "_cache")
FLAG_DIR = os.path.join(CACHE_DIR, "flags")
def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


KEEP_VERSIONS = _int_env("LAW_CACHE_KEEP", 3)

_LOCK = threading.Lock()
STATS = {"hit": 0, "miss": 0, "skip": 0, "error": 0}


def enabled() -> bool:
    env = os.getenv("LAW_CACHE")
    if env is not None and env.strip() != "":
        return env.strip() == "1"
    return os.path.exists(os.path.join(FLAG_DIR, "law_cache"))


def _safe(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣._-]", "_", str(s or ""))[:80] or "_"


def _path(target: str, law_id: str, mst: str, enforced: str) -> str:
    return os.path.join(CACHE_DIR, "laws", _safe(target), _safe(law_id), f"{_safe(mst)}_{_safe(enforced)}.json")


def _good(d) -> bool:
    """조문다운 조문을 받았을 때만 저장(law_client._has_real_articles 와 같은 기준 —
    구조를 못 읽어 만든 '(전문)' 통짜 응답은 저장하지 않고 다음에 다시 받습니다)."""
    if not (isinstance(d, dict) and isinstance(d.get("articles"), list) and d["articles"]):
        return False
    try:
        import law_client
        return bool(law_client._has_real_articles(d))
    except Exception:                       # noqa: BLE001
        arts = d["articles"]
        return any(a.get("조문번호") for a in arts) or sum(len(a.get("조문내용", "")) for a in arts) >= 200


def _digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))[:8]


def _version_ok(d, enforced: str) -> bool:
    """열쇠(검색 결과의 시행일자)와 받은 본문이 같은 판인지. 시행 예정 판(시행일이 오늘 뒤)은 저장하지 않습니다.
    (체계도 등에서 온 MST 가 시행 예정 판이면 '현행' 본문이 그 열쇠로 저장돼 시행 후에도 옛 본문이 남을 수 있음)"""
    import datetime
    e = _digits(enforced)
    if e and e > datetime.date.today().strftime("%Y%m%d"):
        return False
    got = _digits((d.get("meta") or {}).get("시행일자", ""))
    return not (e and got and e != got)


def _prune(folder: str) -> None:
    files = sorted(glob.glob(os.path.join(folder, "*.json")), key=os.path.getmtime, reverse=True)
    for old in files[KEEP_VERSIONS:]:
        try:
            os.remove(old)
        except OSError:
            pass


def get_detail(target: str, law_id: str, mst: str, enforced: str, fetch):
    """
    fetch: 캐시에 없을 때 부를 함수(보통 lambda: law_client.get_detail(target, law_id)).
    MST 를 모르면(검색 결과에 없으면) 캐시하지 않고 그냥 fetch 합니다.
    반환값과 예외는 fetch 와 똑같습니다(캐시 쪽 오류는 삼키고 fetch 로 대신합니다).
    """
    if not enabled() or not law_id or not mst:
        STATS["skip"] += 1
        return fetch()
    path = _path(target, law_id, mst, enforced)
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fp:
                d = json.load(fp)
            if _good(d):
                STATS["hit"] += 1
                return d
            os.remove(path)
    except Exception:                       # noqa: BLE001 — 깨진 캐시는 버리고 새로 받습니다
        STATS["error"] += 1
        try:
            os.remove(path)
        except OSError:
            pass
    d = fetch()                             # 법제처 오류는 그대로 올립니다(기존 동작과 같음)
    STATS["miss"] += 1
    if _good(d) and _version_ok(d, enforced):
        try:
            with _LOCK:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fp:
                    json.dump(d, fp, ensure_ascii=False)
                os.replace(tmp, path)
                _prune(os.path.dirname(path))
        except Exception:                   # noqa: BLE001 — 저장 실패해도 답은 냅니다
            STATS["error"] += 1
    return d


def summary() -> str:
    return f"캐시 적중 {STATS['hit']} · 새로 받음 {STATS['miss']} · 미사용 {STATS['skip']} · 오류 {STATS['error']}"
