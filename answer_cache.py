"""
v1.41 — 같은 질문 결과 불러오기 (되묻기로 묻고, 되묻기 답이 같을 때만 저장본 사용).

흐름
  1) 새 질문(round 0)인데 최근 30일 안에 **같은 질문·같은 참고 법령**으로 끝까지 진행한 기록이 있으면,
     검색·LLM 없이 먼저 묻습니다:
       "10/21 14:30 에 같은 질문을 한 기록이 있습니다. 그때 결과를 불러올까요?"
         [예 (지난 결과 불러오기)] [아니오 (새로 검색)]   ← 기본 선택은 '아니오'
  2) 예 → 그때 그 단계에서 나왔던 결과(되묻기 또는 최종 답)를 그대로 보여 줍니다.
     이후 되묻기마다 **지금까지의 답이 그때와 같으면** 저장본, **하나라도 다르면** 그 단계부터 새로 검색합니다.
  3) 최종 답을 불러올 때는 근거 법령의 현재 버전(MST)을 법제처 검색으로 확인합니다.
     개정됐거나 확인이 안 되면 저장본을 쓰지 않고 새로 검색합니다(화면에 이유 표시).
  4) 아니오·건너뛰기 → 지금과 똑같이 새로 검색합니다. 새 결과가 '가장 최근 기록'이 됩니다.

저장
  _cache/answers/<질문 해시>.json  (PC 안에만, .gitignore). 30일 지난 기록·다른 시스템 버전의 기록은 쓰지 않습니다.
  질문에 개인정보가 섞일 수 있어 공개 저장소·로그로 내보내지 않습니다.

켜고 끄기 (기본 꺼짐)
  환경변수 ANSWER_CACHE=1/0 우선, 없으면 _cache/flags/answer_cache 파일.
  eval_run 은 use_cache=false 로 보내 항상 새로 검색합니다(평가가 저장본으로 통과하지 않게).

원복
  main.py 의 '★ v1.41 답변 캐시' 블록 삭제(원래 한 줄: result = _ask_sync(req, progress)), 이 파일 삭제.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "_cache")
FLAG_DIR = os.path.join(CACHE_DIR, "flags")
TTL_DAYS = 30

MARK = "최근 같은 질문 결과 불러오기"          # 되묻기 질문 머리(답 문자열에서 이것으로 찾아 뗍니다)
OPT_YES = "예 (지난 결과 불러오기)"
OPT_NO = "아니오 (새로 검색)"

_LOCK = threading.Lock()


def enabled() -> bool:
    env = os.getenv("ANSWER_CACHE")
    if env is not None and env.strip() != "":
        return env.strip() == "1"
    return os.path.exists(os.path.join(FLAG_DIR, "answer_cache"))


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _qkey(req) -> str:
    laws = tuple(sorted(str(x).strip() for x in (getattr(req, "laws", None) or []) if str(x).strip()))
    base = json.dumps([_norm(req.question), req.target, laws, req.laws_mode if laws else "",
                       _norm(getattr(req, "prev_question", ""))[:200]], ensure_ascii=False)
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def _path(req) -> str:
    return os.path.join(CACHE_DIR, "answers", _qkey(req) + ".json")


def _split_mark(answered: str):
    """답 문자열에서 '불러오기' 응답을 떼어 냅니다. 반환: (나머지 답, 선택 None/'yes'/'no')."""
    parts = [p for p in re.split(r"\s+/\s+", answered or "") if p.strip()]
    choice, rest = None, []
    for p in parts:
        if p.startswith(MARK):
            choice = "yes" if p.split(":", 1)[-1].strip().startswith("예") else "no"
        else:
            rest.append(p)
    return " / ".join(rest), choice


def _path_key(answered: str, note: str) -> str:
    return _norm(answered) + "|" + _norm(note)


def _load(req):
    p = _path(req)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:                       # noqa: BLE001
        return None


def _fresh(entry, version: str) -> bool:
    return bool(entry) and entry.get("version") == version and \
        time.time() - float(entry.get("t", 0)) < TTL_DAYS * 86400


def _stamp(t: float) -> str:
    return time.strftime("%m/%d %H:%M", time.localtime(t))


def before(req, steps: list, version: str, max_rounds: int):
    """
    _ask_sync 전에 부릅니다. 반환: (응답 dict 또는 None, 상태 dict).
    응답이 None 이면 평소대로 _ask_sync 를 실행하면 됩니다(req.answered/round 는 필요하면 고쳐 둠).
    """
    state = {"mode": "off"}
    try:
        if not enabled() or not getattr(req, "use_cache", True):
            return None, state
        rest, choice = _split_mark(req.answered)
        state["mode"] = "fresh"
        data = _load(req)
        if choice is None:
            # 새 질문 + 되묻기 생략 아님 + 끝까지 간 기록이 있을 때만 물어봅니다.
            if req.round == 0 and not req.skip_clarify and not (req.answered or "").strip() and data:
                final = [v for v in (data.get("paths") or {}).values()
                         if _fresh(v, version) and not (v.get("result") or {}).get("clarify")]
                if final:
                    t = max(float(v["t"]) for v in final)
                    steps.append({"name": "같은 질문 기록 있음", "detail": f"{_stamp(t)} — 불러올지 묻습니다(검색 전)"})
                    return {"clarify": [{"question": f"{MARK}: {_stamp(t)}에 같은 질문을 한 기록이 있습니다. 그때 결과를 불러올까요?",
                                         "options": [OPT_YES, OPT_NO]}],
                            "round": 0, "max_rounds": max_rounds, "answered": "", "steps": steps}, state
            return None, state
        # '불러오기' 응답이 붙은 요청
        req.answered = rest
        if choice == "no":
            if not rest.strip():
                req.round = 0               # 새 질문과 똑같이(되묻기 횟수 손해 없게)
            steps.append({"name": "지난 결과 불러오기", "detail": "아니오 — 새로 검색합니다"})
            return None, state
        entry = ((data or {}).get("paths") or {}).get(_path_key(rest, req.note))
        if not _fresh(entry, version):
            steps.append({"name": "지난 결과 불러오기", "detail": "이번 단계는 그때와 답이 달라 새로 검색합니다"})
            if not rest.strip():
                req.round = 0
            return None, state
        res = json.loads(json.dumps(entry["result"], ensure_ascii=False))
        if not res.get("clarify"):
            ok, why = _laws_unchanged(entry.get("msts") or [])
            if not ok:
                steps.append({"name": "지난 결과 불러오기", "detail": f"{why} — 새로 검색합니다"})
                return None, state
        res["steps"] = steps + [{"name": "지난 결과 불러옴",
                                 "detail": f"{_stamp(float(entry['t']))} 같은 질문·같은 조건의 결과"
                                           + ("" if res.get("clarify") else " · 근거 법령 개정 없음 확인")}]
        steps[:] = res["steps"]
        if res.get("clarify"):
            # 다음 되묻기에서도 '불러오기' 상태가 이어지도록 표시를 붙여 둡니다.
            res["answered"] = " / ".join(x for x in (f"{MARK}: {OPT_YES}", res.get("answered") or "") if x)
        else:
            res.setdefault("warnings", [])
            res["warnings"] = [f"{_stamp(float(entry['t']))}에 같은 질문·같은 조건으로 만든 답변을 불러왔습니다"
                               " (근거 법령 개정 없음 확인). 새 답이 필요하면 '건너뛰고 그냥 검색'이나 질문을 다시 보내세요."] \
                + list(res["warnings"])
        state["mode"] = "replay"
        return res, state
    except Exception as e:                  # noqa: BLE001 — 캐시 문제로 답을 못 내면 안 됩니다
        steps.append({"name": "지난 결과 불러오기 실패(새로 검색)", "detail": str(e)[:120]})
        state["mode"] = "fresh"
        return None, state


def _laws_unchanged(msts: list):
    """저장 당시 근거 법령의 MST 가 지금도 현행인지 법제처 검색으로 확인합니다."""
    if not msts:
        return False, "근거 법령 버전 정보 없음"
    import law_client
    for target, name, mst in msts[:8]:
        try:
            rows = law_client.search(target, name)
        except Exception as e:              # noqa: BLE001
            return False, f"법령 버전 확인 실패({name}: {str(e)[:40]})"
        cur = ""
        for r in rows:
            if _norm(law_client.row_name(target, r)).replace(" ", "") == _norm(name).replace(" ", ""):
                cur = law_client.pick(r, "법령일련번호", "행정규칙일련번호", "자치법규일련번호")
                break
        if not cur:
            return False, f"법령 버전 확인 안 됨({name})"
        if str(cur) != str(mst):
            return False, f"근거 법령 개정됨({name})"
    return True, ""


def after(req, result, state: dict, version: str) -> None:
    """새로 검색한 결과를 '가장 최근 기록'으로 저장합니다(불러온 결과·오류·범위 밖은 저장하지 않음)."""
    try:
        if state.get("mode") != "fresh" or not isinstance(result, dict):
            return
        if result.get("out_of_scope") or (not result.get("clarify") and not result.get("answer")):
            return
        if result.get("clarify") and any(str(c.get("question", "")).startswith(MARK)
                                         for c in result["clarify"]):
            return
        msts = []
        for f in result.get("laws") or []:
            if isinstance(f, dict) and f.get("mst") and f.get("name") and f.get("target"):
                msts.append((f["target"], f["name"], str(f["mst"])))
        cited = {str(k).split(" 제")[0] for k in (result.get("cited_keys") or [])}
        if cited:                           # 실제 인용한 법령을 먼저(확인 호출 수를 줄임)
            msts.sort(key=lambda x: 0 if x[1] in cited else 1)
        entry = {"t": time.time(), "version": version, "result": result, "msts": msts[:8]}
        with _LOCK:
            data = _load(req) or {"question": _norm(req.question), "paths": {}}
            data["paths"][_path_key(req.answered, req.note)] = entry
            # 오래된 기록 정리
            data["paths"] = {k: v for k, v in data["paths"].items()
                             if time.time() - float(v.get("t", 0)) < TTL_DAYS * 86400}
            p = _path(req)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False, default=str)
            os.replace(tmp, p)
    except Exception:                       # noqa: BLE001 — 저장 실패해도 답은 이미 나갔습니다
        pass
