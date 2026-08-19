"""
법제처 국가법령정보 OPEN API 클라이언트.

C#으로 치면 HttpClient를 감싼 서비스 클래스입니다.
파이썬은 인터페이스/DI가 없어도 그냥 함수로 두고 쓰는 경우가 많습니다.

[검증 상태]
  - lawSearch.do (목록 조회)  : 직접 호출 확인 완료
  - lawService.do (본문 조회) : 직접 호출 확인 완료
  - XML 응답의 태그 이름       : 미확인. dump_raw() 로 직접 보고 확정할 것.
"""

import os
import re
import xml.etree.ElementTree as ET

import httpx

# --- 상수 -------------------------------------------------------------
# 법제처는 http 입니다 (https 아님). 나중에 https 로 바뀌면 여기만 고치면 됩니다.
SEARCH_URL = "http://www.law.go.kr/DRF/lawSearch.do"
SERVICE_URL = "http://www.law.go.kr/DRF/lawService.do"

# 환경변수에서 인증키를 읽습니다. 코드에 직접 쓰지 마세요.
# C#의 appsettings.json / Java의 application.properties 자리라고 보시면 됩니다.
OC = os.getenv("LAW_OC", "")

# target 파라미터 값. 필요한 것만 추려놨습니다.
# 브라우저에서 하나씩 눌러보고 실제로 뭐가 나오는지 확인하세요.
# ── 법령 조회 기준 ────────────────────────────────────────────────
# 법제처는 같은 법령을 두 가지 기준으로 제공합니다.
#   law   : 현행법령(공포일) — lawSearch.do?target=law
#   eflaw : 현행법령(시행일) — lawSearch.do?target=eflaw
#
# 공포일 기준은 "공포된 것" 이고, 시행일 기준은 "지금 효력이 있는 것" 입니다.
# 개정법이 공포됐지만 시행일이 아직 안 온 경우 둘이 갈립니다.
# 민원 안내는 오늘 효력 있는 조문으로 해야 하므로 시행일 기준을 기본으로 씁니다.
#
# 문제가 생기면 .env 에 LAW_TARGET=law 로 즉시 되돌릴 수 있습니다.
LAW_TARGET = (os.getenv("LAW_TARGET", "eflaw").strip() or "eflaw")
if LAW_TARGET not in ("law", "eflaw"):
    LAW_TARGET = "eflaw"

TARGETS = {
    "law": "법령(공포일 기준)",
    "eflaw": "법령(시행일 기준)",
    "admrul": "행정규칙(고시·훈령·예규·지침)",
    "ordin": "자치법규(조례·규칙)",
    "expc": "법령해석례",
    "lsStmd": "법령체계도",
    "lsHistory": "법령 연혁",
    # ★ 별표·서식은 본문 조회에 딸려오지 않습니다. 별도 target 입니다.
    #   dump_raw() 로 확인할 수 있게 여기에도 넣어둡니다.
    "licbyl": "법령 별표·서식 목록",
    "admbyl": "행정규칙 별표·서식 목록",
    "ordinbyl": "자치법규 별표·서식 목록",
}

# 본문 조회(lawService)에서 별표가 함께 오는지 여부.
#   행정규칙 : <별표단위> 가 본문에 함께 옵니다.
#   법령     : 오지 않습니다. "별표 4와 같다" 로 넘겨도 내용을 알 수 없어
#              AI 가 검사주기 같은 수치를 지어내는 원인이 됩니다.
#              그래서 아래 get_byeolpyo() 로 따로 가져옵니다.
BYL_TARGET = {"law": "licbyl", "eflaw": "licbyl",
              "admrul": "admbyl", "ordin": "ordinbyl"}

# target 별 ID 후보. 본문 조회(lawService)에 넘길 값입니다.
# ★ 행정규칙은 "행정규칙ID"가 아니라 "행정규칙일련번호"를 써야 합니다.
ID_KEYS = {
    "law": ["법령ID", "법령일련번호", "ID"],
    # 시행일 기준도 같은 키를 씁니다. 응답 필드 이름이 동일합니다.
    "eflaw": ["법령ID", "법령일련번호", "ID"],
    "admrul": ["행정규칙일련번호", "행정규칙ID"],
    # ★ MST 파라미터는 자치법규ID 를 받습니다.
    #   자치법규일련번호를 넣으면 에러 없이 "다른 지자체 조례"가 반환됩니다. 순서 주의.
    "ordin": ["자치법규ID", "자치법규일련번호"],
    "expc": ["법령해석례일련번호", "안건번호"],
    "lsStmd": ["법령일련번호", "법령ID"],
}

# ★ target 별 본문조회 파라미터 이름.
#   자치법규만 ID 가 아니라 MST 를 씁니다. (응답의 상세링크에서 확인)
DETAIL_PARAM = {"law": "ID", "eflaw": "ID", "admrul": "ID",
                "ordin": "MST", "expc": "ID", "lsStmd": "MST"}

# target 별 이름 후보
NAME_KEYS = {
    "law": ["법령명_한글", "법령명한글", "법령명"],
    "eflaw": ["법령명_한글", "법령명한글", "법령명"],
    "admrul": ["행정규칙명"],
    "ordin": ["자치법규명"],
    "expc": ["안건명"],
    "lsStmd": ["법령명", "법령명_한글"],
}

# 요청 타임아웃(초). 법제처가 느릴 때가 있어 넉넉히 잡았습니다.
TIMEOUT = 20.0

# ── 목록 조회 파라미터 제약 (매뉴얼 대조 결과) ──────────────────────
# display 는 매뉴얼상 max=100 입니다. 넘겨도 조용히 잘리거나 무시되므로
# 호출 전에 우리가 잘라서 "몇 건 받았는지" 판정이 어긋나지 않게 합니다.
MAX_DISPLAY = 100

# `search`(검색범위 1 법령명 / 2 본문검색) 파라미터가 스펙에 없는 target.
#   · lsStmd (법령 체계도 목록)  — 요청변수 목록에 search 없음
#   · lsHistory (법령 연혁 목록) — 요청변수 목록에 search 없음
#   · lsAbrv (법령명 약칭)       — 요청변수가 OC/target/type/stdDt/endDt 뿐
# 스펙에 없는 파라미터를 보내면 무시되거나 엉뚱하게 동작합니다.
NO_SCOPE_TARGETS = {"lsStmd", "lsHistory", "lsAbrv"}

# `page` 파라미터가 스펙에 없는 target. (lsAbrv 는 페이징을 지원하지 않습니다)
NO_PAGE_TARGETS = {"lsAbrv"}

# XML 대신 HTML 만 돌려주는 target. `_get()` 은 type=XML 을 고정으로 보내므로
# 이 target 들은 현재 파서로 처리할 수 없습니다.
#   매뉴얼: 법령 연혁 목록/본문 조회 → "출력 형태 HTML" (XML/JSON 없음)
HTML_ONLY_TARGETS = {"lsHistory"}


def _clamp_display(display) -> int:
    """display 를 1~100 으로 조정합니다. 잘못된 값이면 기본값 20."""
    try:
        d = int(display)
    except (TypeError, ValueError):
        return 20
    return max(1, min(d, MAX_DISPLAY))


class LawApiError(Exception):
    """법제처 호출 실패. FastAPI 쪽에서 잡아서 사용자에게 보여줍니다."""


def _require_oc() -> str:
    if not OC:
        raise LawApiError(
            "인증키가 없습니다. .env 파일에 LAW_OC=발급받은키 를 넣고 서버를 다시 시작하세요."
        )
    return OC


def _get(url: str, params: dict) -> str:
    """공통 GET 호출. 응답 본문(문자열)을 그대로 돌려줍니다."""
    params = {"OC": _require_oc(), "type": "XML", **params}
    try:
        # httpx 는 requests 와 거의 같은데 async 를 지원해서 FastAPI 와 궁합이 좋습니다.
        resp = httpx.get(url, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise LawApiError(f"법제처 호출 실패: {e}") from e

    # 법제처가 인증 실패 시에도 HTTP 200 에 에러 문구를 담아 보내는 경우가 있습니다.
    if "인증" in resp.text and len(resp.text) < 300:
        raise LawApiError(f"인증키 문제로 보입니다. 응답: {resp.text[:200]}")

    return resp.text


def _elem_to_dict(elem: ET.Element) -> dict:
    """
    XML 엘리먼트 하나를 dict 로 변환.

    태그 이름을 모르는 상태이므로, 자식 노드를 전부 긁어옵니다.
    실제 응답을 보고 필요한 필드만 골라내도록 나중에 좁히세요.
    """
    out = {}
    for child in elem:
        text = (child.text or "").strip()
        if text:
            out[child.tag] = text
    return out


def _parse_search_rows(root: ET.Element) -> tuple[list[dict], int]:
    """
    목록 조회 응답에서 (레코드 목록, totalCnt) 를 뽑습니다.

    루트 바로 아래 반복되는 엘리먼트들이 결과 목록입니다.
    태그 이름(law / admrul / ...)이 target 마다 달라서, 이름으로 찾지 않고
    "자식이 있는 엘리먼트"를 전부 결과로 봅니다.
    totalCnt / page / target 등은 잎 노드라 자연히 걸러집니다.
    """
    try:
        total = int((root.findtext("totalCnt") or "0").strip() or 0)
    except ValueError:
        total = 0

    results = []
    for child in root:
        if len(child) == 0:
            continue
        row = _elem_to_dict(child)
        # 폐지·연혁본 제외. 이 필드가 없으면 통과시킵니다.
        # ★ 실제 필드명은 "현행연혁코드" 입니다. "현행연혁구분" 은 실측 없이
        #   넣었던 이름이라 지금까지 이 필터가 계속 no-op 이었습니다.
        #   (2026-08-18 /api/raw 실측: <현행연혁코드>현행</현행연혁코드> /
        #    <현행연혁코드>연혁</현행연혁코드> — 값도 정확히 이 두 문자열)
        #   eflaw 는 검색 시 nw=3 을 같이 보내 서버가 현행만 걸러주므로
        #   증상이 안 보였지만, law 등 다른 target 은 연혁본이 섞여 들어갈
        #   수 있었습니다.
        if row.get("현행연혁코드", "현행") != "현행":
            continue
        results.append(row)
    return results, total


def search(target: str, query: str, display: int = 20, scope: int = 1,
           page: int = 1) -> list[dict]:
    """
    목록 조회. 예) search("eflaw", "토양환경보전법")

    scope — 검색 범위. 법제처 파라미터 이름은 `search` 입니다.
        1 : 법령명에서 찾기 (기본)
        2 : **본문에서 찾기**

    scope=2 가 중요합니다. 이 도구는 AI 가 추측한 법령명으로 검색하는데,
    추측이 빗나가면 그 뒤 단계가 전부 엉뚱한 법령 위에서 돌아갑니다.
    본문 검색은 법령명을 몰라도 "누출검사주기" 라는 말이 실제로 들어간
    법령을 찾아주므로, 추측 실패를 복구하는 경로가 됩니다.

    display — 매뉴얼상 max=100. 넘겨도 서버가 잘라버리므로 우리가 먼저
        1~100 으로 조정합니다. 안 그러면 search_all() 의 "마지막 페이지"
        판정("받은 개수 < display")이 영원히 참이 되지 않습니다.

    page — 검색 결과 페이지(기본 1). 매뉴얼상 lsAbrv 만 미지원입니다.

    반환: [{태그: 값, ...}, ...]
    """
    if target not in TARGETS:
        raise LawApiError(f"지원하지 않는 target: {target}")
    if target in HTML_ONLY_TARGETS:
        # type=XML 을 보내도 HTML 이 돌아와 ET.fromstring 이 반드시 깨집니다.
        # "XML 파싱 실패" 로 흘려보내지 말고 원인을 그대로 말해 줍니다.
        raise LawApiError(
            f"target={target} 은 법제처가 HTML 만 제공합니다(매뉴얼: 출력 형태 HTML). "
            f"XML 파서로는 처리할 수 없습니다."
        )

    display = _clamp_display(display)
    params = {"target": target, "query": query, "display": display}

    if scope and int(scope) != 1:
        if target in NO_SCOPE_TARGETS:
            # 스펙에 없는 파라미터입니다. 보내면 무시되거나 오동작하므로 뺍니다.
            # (예: lsStmd 에 search=2 를 주면 검색범위가 아니라 잡음이 됩니다)
            pass
        else:
            params["search"] = str(int(scope))

    try:
        page_no = max(1, int(page))
    except (TypeError, ValueError):
        page_no = 1
    if page_no > 1 and target not in NO_PAGE_TARGETS:
        params["page"] = str(page_no)

    # 시행일 기준 목록만 nw 를 받습니다. 3 = 현행 (1 연혁, 2 시행예정).
    # 서버가 걸러주므로 연혁본·시행예정본이 아예 안 넘어옵니다.
    if target == "eflaw":
        params["nw"] = "3"

    xml_text = _get(SEARCH_URL, params)

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise LawApiError(f"XML 파싱 실패: {e}\n앞부분: {xml_text[:300]}") from e

    rows, _total = _parse_search_rows(root)
    return rows


def search_all(target: str, query: str, scope: int = 1,
               max_items: int = 300, per_page: int = MAX_DISPLAY) -> list[dict]:
    """
    여러 페이지를 이어 받습니다. 100건이 넘는 검색 결과가 필요할 때 씁니다.

    종료 조건을 두 개 겹쳐 둡니다 — 하나만 믿으면 무한 루프가 납니다.
      (a) 이번 페이지에서 받은 원본 레코드 수 < per_page  → 마지막 페이지
      (b) 모아둔 개수 >= max_items 또는 >= totalCnt        → 충분히 받음
    (a) 의 판정 대상은 **필터 전** 개수입니다. 연혁본이 걸러져 개수가 줄면
    아직 뒷페이지가 남았는데도 끝난 줄 알기 때문입니다.

    max_items — 안전장치. 기본 300건.
    """
    if target in NO_PAGE_TARGETS:
        return search(target, query, display=per_page, scope=scope)

    # ★ 2026-08-19 — 종료 조건 세 개가 서로 어긋나 **무한 루프**가 났습니다.
    #   while 은 필터 **후** 개수(out)를 보는데, (a) 는 필터 **전** 개수를 보고,
    #   (b) 는 totalCnt 가 없으면 영원히 성립하지 않습니다.
    #   그래서 "100건이 전부 연혁본" 인 페이지가 오면 out 이 하나도 안 늘어난 채
    #   같은 요청을 끝없이 반복했습니다(실측: 501회 호출 후 강제 중단).
    #   → 페이지 번호로 상한을 걸고, 한 페이지에서 한 건도 못 늘리면 멈춥니다.
    #     서버가 page 를 무시하고 같은 결과를 줄 때를 대비해 중복도 거릅니다.
    per_page = _clamp_display(per_page)
    out: list[dict] = []
    total = None
    page_no = 1
    max_pages = max(1, (max_items + per_page - 1) // per_page + 2)
    seen_rows: set = set()

    while len(out) < max_items and page_no <= max_pages:
        params = {"target": target, "query": query, "display": per_page}
        if scope and int(scope) != 1 and target not in NO_SCOPE_TARGETS:
            params["search"] = str(int(scope))
        if page_no > 1:
            params["page"] = str(page_no)
        if target == "eflaw":
            params["nw"] = "3"

        try:
            root = ET.fromstring(_get(SEARCH_URL, params))
        except ET.ParseError:
            break

        rows, page_total = _parse_search_rows(root)
        raw_count = sum(1 for c in root if len(c) > 0)   # 필터 전 개수
        if total is None and page_total:
            total = page_total

        # 서버가 page 를 무시하고 같은 페이지를 계속 줄 수 있습니다. 중복 제거.
        added = 0
        for r in rows:
            key = (row_id(target, r) or "", row_name(target, r) or "")
            if key in seen_rows:
                continue
            seen_rows.add(key)
            out.append(r)
            added += 1

        if raw_count == 0:                       # 빈 페이지
            break
        if added == 0:                           # 새로 얻은 게 없으면 진전 없음
            break
        if raw_count < per_page:                 # 마지막 페이지
            break
        if total and page_no * per_page >= total:  # totalCnt 도달
            break
        page_no += 1

    return out[:max_items]


def row_id(target: str, row: dict) -> str:
    """목록 레코드에서 본문 조회용 ID를 꺼냅니다."""
    return pick(row, *ID_KEYS.get(target, ["ID"]))


def row_name(target: str, row: dict) -> str:
    """목록 레코드에서 표시용 이름을 꺼냅니다."""
    return pick(row, *NAME_KEYS.get(target, ["명칭"]))


def get_detail(target: str, law_id: str) -> dict:
    """
    본문 조회. 시행일 기준(eflaw)이 비면 공포일 기준(law)으로 되돌립니다.

    문서상 eflaw 는 ID 만으로 현행 본문을 주지만, 실제로 조문을 못 받는
    법령이 있을 수 있습니다. 그때 빈손으로 끝내면 답변 근거가 통째로
    사라지므로, 조문이 하나도 없으면 조용히 공포일 기준으로 다시 받습니다.
    """
    out = _get_detail_once(target, law_id)
    if target == "eflaw" and not _has_real_articles(out):
        try:
            alt = _get_detail_once("law", law_id)
        except LawApiError:
            return out
        if _has_real_articles(alt):
            # 어느 기준으로 받았는지 남깁니다. 화면·로그에서 구분할 수 있어야 합니다.
            alt.setdefault("meta", {})["조회기준"] = "공포일(시행일 기준 조회가 비어 대체)"
            return alt
    elif target == "eflaw":
        out.setdefault("meta", {})["조회기준"] = "시행일"
    return out


def _has_real_articles(d: dict) -> bool:
    """
    조문다운 조문을 받았는지 판정합니다.

    ★ `articles` 가 비었는지만 보면 안 됩니다.
      _extract_articles() 는 구조를 못 읽으면 마지막에 "(전문)" 이라는
      통짜 항목 하나를 만들어 넣습니다. 그래서 응답이 사실상 비어 있어도
      articles 는 1건이 되어, 되돌리기가 동작하지 않습니다.
    """
    arts = d.get("articles") or []
    if not arts:
        return False
    if any(a.get("조문번호") for a in arts):
        return True
    # 조문번호가 하나도 없으면 "(전문)" 통짜일 가능성이 큽니다.
    # 그래도 내용이 충분히 길면 행정규칙 평문일 수 있으니 살립니다.
    return sum(len(a.get("조문내용", "")) for a in arts) >= 200


def _get_detail_once(target: str, law_id: str) -> dict:
    """
    본문 조회 한 번.

    법령은 <법령><조문><조문단위> 구조가 확인됐습니다.
    행정규칙·자치법규·해석례는 구조가 다를 수 있어 방어적으로 훑습니다.
    """
    key = DETAIL_PARAM.get(target, "ID")
    xml_text = _get(SERVICE_URL, {"target": target, key: law_id})

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise LawApiError(f"XML 파싱 실패: {e}\n앞부분: {xml_text[:300]}") from e

    # --- 기본정보 (여러 이름 대응) ---------------------------------
    meta = {}
    for tag in ("기본정보", "행정규칙기본정보", "자치법규기본정보", "조례기본정보", "해석례기본정보", "해석례정보"):
        basic = root.find(f".//{tag}")
        if basic is not None:
            for c in basic:
                t = (c.text or "").strip()
                if t:
                    meta[c.tag] = t
            break
    # 기본정보 묶음이 없으면 루트의 잎 노드를 메타로
    if not meta:
        for c in root:
            if len(c) == 0 and (c.text or "").strip():
                meta[c.tag] = c.text.strip()

    articles = _extract_articles(root)
    return {"meta": meta, "articles": articles}


# 평문 조문에서 "제3조의2(제목)" 을 뽑는 패턴
_TITLE_RE = re.compile(r"^\s*제\s*\d+\s*조(?:\s*의\s*\d+)?\s*\(([^)]{1,60})\)")


def _clean_body(text: str) -> str:
    """
    조문 본문에서 화면·PDF 에 그대로 나오면 안 되는 것만 걷어냅니다.
      · <img id="163356319"></img>  — 법제처가 표·그림을 이미지로 넣은 자리
    담당부서·담당자·전화번호·첨부파일 링크는 법제처가 공개하는 정보이고
    실무에 쓸모가 있으므로 그대로 둡니다.
    """
    t = str(text or "")
    t = re.sub(r"<img[^>]*>(?:\s*</img>)?", "[그림·표 생략]", t, flags=re.I)
    t = re.sub(r"</?img[^>]*>", "", t, flags=re.I)
    t = re.sub(r"\s{3,}", " ", t)
    return t.strip()


def _title_from_body(body: str) -> str:
    """본문 첫머리 "제64조(정기점검의 횟수)" 에서 괄호 안 제목을 뽑습니다."""
    m = _TITLE_RE.match(body or "")
    return m.group(1).strip() if m else ""


_ART_RE = re.compile(r"^\s*제\s*(\d+)\s*조(?:\s*의\s*(\d+))?\s*(?:\(([^)]{1,80})\))?")


def _extract_articles(root: ET.Element) -> list[dict]:
    """
    조문 목록 추출.

    두 가지 구조를 지원합니다.
      (A) 법령      : <조문>/<조문단위> 안에 조문번호·조문제목 태그가 있음
      (B) 행정규칙  : <조문내용> 이 루트 바로 아래 평평하게 나열. 태그 없음.
                      본문 텍스트에서 "제3조(설치신고)" 를 정규식으로 뽑아야 함.
    """
    articles = []

    # ---- (A) 법령 구조 ------------------------------------------
    for unit in root.findall(".//조문/조문단위"):
        def g(tag: str) -> str:
            el = unit.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""

        content = g("조문내용")
        # 조문제목 태그가 비어 있는 법령이 있습니다.
        # 그때는 본문 첫머리의 "제64조(정기점검의 횟수)" 에서 제목을 뽑습니다.
        title = g("조문제목") or _title_from_body(content)
        # 항 → 호 → 목 순서로 붙입니다.
        # ★ 법제처 XML 에서 <목> 은 <호> 안이 아니라 <항> 바로 아래 형제로 있습니다.
        #   그래서 호만 훑으면 "가. 나. 다." 목이 통째로 빠집니다.
        #   문서 순서(document order)대로 읽어야 목이 해당 호 뒤에 제자리로 들어갑니다.
        for hang in unit.findall("항"):
            h = hang.find("항내용")
            if h is not None and h.text:
                content += "\n" + h.text.strip()
            for child in hang:                       # 문서에 적힌 순서대로
                if child.tag == "호":
                    t = child.findtext("호내용")
                    if t:
                        content += "\n" + t.strip()
                elif child.tag == "목":
                    t = child.findtext("목내용")
                    if t:
                        content += "\n  " + t.strip()   # 목은 한 단 들여씁니다
        if not content:
            continue
        articles.append({
            "조문번호": g("조문번호"), "조문가지번호": g("조문가지번호"),
            "조문제목": title, "조문여부": g("조문여부"),
            "시행일자": g("조문시행일자"), "조문내용": _clean_body(content), "구분": "조문",
        })

    # ---- (A-2) 자치법규 구조 : <조문>/<조> --------------------
    if not articles:
        for jo in root.findall(".//조문/조"):
            def jg(tag: str) -> str:
                el = jo.find(tag)
                return (el.text or "").strip() if el is not None and el.text else ""
            body = jg("조내용")
            if not body:
                continue
            # 조문번호가 '000100' 처럼 0 패딩 6자리입니다. 앞 4자리가 조, 뒤 2자리가 가지번호.
            raw = jg("조문번호")
            no, gaji = "", ""
            if raw.isdigit() and len(raw) >= 5:
                no = str(int(raw[:-2]))
                g = int(raw[-2:])
                gaji = str(g) if g else ""
            else:
                m = _ART_RE.match(body)
                if m:
                    no, gaji = m.group(1), m.group(2) or ""
            articles.append({
                "조문번호": no, "조문가지번호": gaji,
                "조문제목": jg("조제목") or _title_from_body(body),
                "조문여부": "조문", "시행일자": "", "조문내용": _clean_body(body), "구분": "조문",
            })

    # ---- (B) 행정규칙 평문 구조 ------------------------------
    if not articles:
        for el in root.findall("조문내용") or root.findall(".//조문내용"):
            text = (el.text or "").strip()
            if not text:
                continue
            m = _ART_RE.match(text)
            if m:
                no, gaji, title = m.group(1), m.group(2) or "", m.group(3) or ""
                yn = "조문"
            else:
                # "제1장 총칙" 같은 장·절 제목
                no, gaji, title, yn = "", "", "", "전문"
            articles.append({
                "조문번호": no, "조문가지번호": gaji, "조문제목": title,
                "조문여부": yn, "시행일자": "", "조문내용": _clean_body(text), "구분": "조문",
            })

    # ---- 별표·별지 (행정규칙은 본문 텍스트가 함께 옵니다) ---------
    for bp in root.findall(".//별표단위"):
        def bg(tag: str) -> str:
            el = bp.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        # 별표내용은 CDATA 여러 조각으로 나뉘어 있어 itertext 로 합칩니다.
        body_el = bp.find("별표내용")
        body = " ".join(t.strip() for t in body_el.itertext() if t and t.strip()) if body_el is not None else ""
        title = bg("별표제목")
        if not (title or body):
            continue
        if not body:
            body = "(본문이 첨부파일로만 제공됩니다. 원본을 내려받아 확인하세요.)"
        kind = bg("별표구분") or "별표"
        # ★ 2026-08-19 — 여기만 옛 방식(lstrip)이 남아 있었습니다. 같은 파일의
        #   _byl_no() 는 6자리 0패딩(000400 = 별표 4)을 제대로 풀면서
        #   "lstrip 으로 자르면 000400 이 400 이 된다" 고 경고까지 해 두었는데,
        #   이 줄은 그 경고 그대로 밟고 있었습니다. 두 파서가 같은 필드를 다르게
        #   읽으니 어느 형식이든 한쪽은 반드시 틀립니다.
        #   실제 피해: 제목이 "[별표 400]" 이 되면 main.py 가 본문의 "별표 4" 와
        #   못 맞춰서, **이미 받아 온 별표를 "못 받았다" 고 판정**하고
        #   "수치를 확인할 수 없다" 경고를 띄웁니다. 정작 주기는 응답 안에
        #   들어 있었는데 버려집니다.
        #   가지번호도 함께 읽습니다 — 안 읽으면 별표 6 과 별표 6의3 이 둘 다
        #   "[별표 6]" 이 되어 서로를 덮어씁니다.
        _bno, _bgaji = _byl_no(bg("별표번호"))
        _label = (_bno + (f"의{_bgaji}" if _bgaji else "")) or "?"
        articles.append({
            "조문번호": "", "조문가지번호": "",
            "조문제목": f"[{kind} {_label}] {title}",
            "조문여부": "조문", "시행일자": "", "구분": kind,
            "조문내용": body[:6000],
            "파일링크": bg("별표서식PDF파일링크") or bg("별표서식파일링크") or bg("별표첨부파일명"),
        })

    # ---- 그래도 비면 전체 텍스트 ---------------------------------
    if not articles:
        # 구조를 못 읽은 경우. 담당부서·담당자 등 기본정보도 실무에 쓸모가 있으므로
        # 전체 텍스트를 그대로 씁니다. (이미지 태그만 정리)
        blob = _clean_body(" ".join(t.strip() for t in root.itertext() if t and t.strip()))
        if blob:
            articles.append({"조문번호": "", "조문가지번호": "", "조문제목": "(전문)",
                             "조문여부": "조문", "시행일자": "", "구분": "조문",
                             "조문내용": blob[:20000]})
    return articles


# 「」 안의 다른 법령 이름을 뽑습니다. 환경↔토목 연결 지점 탐지용.
_REF_RE = re.compile(r"「([^」]{2,60})」")


def extract_references(articles: list[dict], self_name: str = "") -> list[str]:
    names = {}
    for a in articles:
        for m in _REF_RE.findall(a.get("조문내용", "")):
            m = m.strip()
            if not m or m in self_name or self_name in m:
                continue
            names[m] = names.get(m, 0) + 1
    return [n for n, _ in sorted(names.items(), key=lambda x: -x[1])]


def _byl_no(raw: str) -> tuple[str, str]:
    """
    별표번호를 (번호, 가지번호) 로 풉니다.

    법제처는 6자리 0패딩으로 줍니다. 앞 4자리가 번호, 뒤 2자리가 가지번호입니다.
      000400 → 별표 4        (실측: 특정토양오염관리대상시설의 토양오염검사주기)
      000603 → 별표 6의3
      001102 → 별표 11의2

    ★ lstrip("0") 로 자르면 000400 이 "400" 이 됩니다. 절대 안 맞습니다.
    """
    raw = str(raw or "").strip()
    if raw.isdigit() and len(raw) >= 5:
        no = str(int(raw[:-2]))
        g = int(raw[-2:])
        return no, (str(g) if g else "")
    return raw.lstrip("0"), ""


def _norm_name(s: str) -> str:
    return re.sub(r"\s+", "", str(s or ""))


def get_byeolpyo(target: str, law_id: str = "", law_name: str = "") -> list[dict]:
    """
    별표·서식 목록을 따로 가져옵니다.

    왜 필요한가:
      시행규칙 제12조제2항은 "누출검사주기는 별표 4와 같다" 라고만 정합니다.
      실제 주기(몇 년마다)는 별표 4 에 있는데, 법령 본문 조회(target=law)에는
      별표가 딸려오지 않습니다. 그대로 두면 AI 가 없는 숫자를 지어냅니다.

    [실측으로 확정한 것]
      · 검색범위 파라미터는 search=2 (법령명). section=lawNm 은 먹지 않습니다.
        안 주면 기본값이 별표명 검색이라 0건이 나옵니다.
      · ★ MST 는 무시됩니다. MST=281911 을 줘도 그 값을 버리고 query 로만 찾아
        병역법·출입국관리법 같은 전혀 다른 법령의 별표가 돌아옵니다.
        그래서 MST 는 쓰지 않고, 받은 결과를 법령명으로 한 번 더 거릅니다.
      · 응답 필드 이름은 별표명·별표종류·관련법령명 입니다
        (별표제목·별표구분·법령명 이 아닙니다).
      · 본문 텍스트(별표내용)는 오지 않습니다. 파일 링크만 옵니다.

    반환: [{"kind": "별표", "no": "4", "gaji": "", "title": "...",
            "body": "", "link": "https://...", "law": "...", "detail": "..."}]
    """
    byl = BYL_TARGET.get(target)
    if not byl or not law_name:
        return []

    try:
        xml_text = _get(SEARCH_URL, {
            "target": byl, "query": law_name, "search": "2", "display": 100})
        root = ET.fromstring(xml_text)
    except (LawApiError, ET.ParseError):
        # 별표는 있으면 좋고 없으면 마는 보조 정보입니다.
        # 여기서 예외를 올리면 조회 전체가 실패하므로 조용히 넘어갑니다.
        return []

    rows = _parse_byeolpyo(root)

    # ★ 반드시 법령명으로 한 번 더 거릅니다.
    #   search=2 는 부분일치입니다. "토양환경보전법" 으로 찾으면
    #   시행령·시행규칙 별표가 함께 돌아와, 법률 별표 자리에 시행규칙 별표가
    #   들어가는 사고가 납니다. 조문 인용은 법령이 다르면 완전히 다른 규정입니다.
    want = _norm_name(law_name)
    exact = [r for r in rows if _norm_name(r.get("law", "")) == want]
    return exact if exact else []


def _parse_byeolpyo(root: ET.Element) -> list[dict]:
    """별표·서식 목록 응답에서 필요한 항목만 뽑습니다."""
    out = []
    for child in root:
        if len(child) == 0:
            continue
        row = _elem_to_dict(child)
        no, gaji = _byl_no(pick(row, "별표번호"))
        # 응답 필드는 별표명·별표종류 입니다. 옛 이름도 후보로 남겨둡니다.
        title = pick(row, "별표명", "별표제목")
        if not (no or title):
            continue
        # 삭제된 별표는 쓸모가 없습니다. ("삭제 <2005.6.30>")
        if title.startswith("삭제"):
            continue
        # 내려받기용 첨부 (HWP/PDF)
        f_link = pick(row, "별표서식PDF파일링크", "별표서식파일링크")
        if f_link and f_link.startswith("/"):
            f_link = "https://www.law.go.kr" + f_link

        # ★ 브라우저에서 표를 바로 볼 수 있는 주소.
        #   목록의 별표법령상세링크(lawService.do?…&type=HTML)를 열면
        #   iframe 껍데기만 옵니다. 실제 표는 그 안쪽 lsBylInfoP.do 에 있습니다.
        #   필요한 두 일련번호가 목록 응답에 이미 있으므로 직접 만듭니다.
        #   (추가 호출도 스크래핑도 아닙니다. 사람이 눌러서 볼 주소만 만드는 것)
        seq = pick(row, "별표일련번호")
        lsi = pick(row, "관련법령일련번호")
        view = (f"https://www.law.go.kr/LSW/lsBylInfoP.do?bylSeq={seq}&lsiSeq={lsi}"
                if seq and lsi else "")

        # 본문 텍스트는 오지 않는 것으로 확인했습니다. 혹시 오면 씁니다.
        body_el = child.find("별표내용")
        body = ""
        if body_el is not None:
            body = " ".join(t.strip() for t in body_el.itertext() if t and t.strip())
        out.append({
            "kind": pick(row, "별표종류", "별표구분") or "별표",
            "no": no, "gaji": gaji, "title": title,
            "body": _clean_body(body),
            # link 는 "사람에게 보여줄 대표 주소" 입니다.
            # 화면에서 바로 읽히는 쪽을 우선합니다. 첨부는 내려받아야 열립니다.
            "link": view or f_link,
            "view": view, "file": f_link, "seq": seq,
            "law": pick(row, "관련법령명", "법령명", "법령명_한글"),
        })
    return out


def dump_raw(target: str, query_or_id: str, mode: str = "search",
             extra: dict | None = None) -> str:
    """
    파싱하지 않은 원본 XML 을 그대로 반환.

    태그 이름을 확인할 때 쓰세요. 브라우저에서 URL 열어보는 것과 같습니다.

    extra — 법제처에 그대로 넘길 추가 파라미터.
            문서에 없는 파라미터 이름을 시험해 볼 때 씁니다.
            예) {"search": "2"} / {"section": "lawNm"} / {"MST": "281911"}
    """
    if mode == "search":
        params = {"target": target, "query": query_or_id, **(extra or {})}
        return _get(SEARCH_URL, params)
    params = {"target": target, DETAIL_PARAM.get(target, "ID"): query_or_id,
              **(extra or {})}
    return _get(SERVICE_URL, params)


def pick(d: dict, *candidates: str, default: str = "") -> str:
    """
    dict 에서 후보 키들을 순서대로 찾아 첫 번째로 있는 값을 반환.

    태그 이름이 확실치 않으니, 있을 법한 이름을 여러 개 넣어두고 씁니다.
    예) pick(row, "법령명한글", "법령명", "행정규칙명")
    """
    for key in candidates:
        if key in d and d[key]:
            return d[key]
    return default


# =====================================================================
# 법령체계도 — 법률/시행령/시행규칙 + 각 단계에 위임된 행정규칙
# =====================================================================
def get_hierarchy(law_mst: str) -> dict:
    """
    target=lsStmd 로 체계도를 조회합니다.

    응답 구조:
      <법령체계도><상하위법><법률>
          <기본정보>…법률…</기본정보>
          <시행령><기본정보>…</기본정보>
              <시행규칙><기본정보>…</기본정보>
                  <행정규칙><예규>…</예규><고시>…</고시></행정규칙>
              </시행규칙>
              <행정규칙>…시행령에 위임된 고시…</행정규칙>
          </시행령>
          <행정규칙>…법률에 위임된 고시…</행정규칙>
      </법률></상하위법></법령체계도>

    반환: {"laws": [법률·시행령·시행규칙], "admruls": [위임행정규칙]}
    """
    xml_text = _get(SERVICE_URL, {"target": "lsStmd", "MST": law_mst})
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise LawApiError(f"체계도 파싱 실패: {e}\n앞부분: {xml_text[:300]}") from e

    laws, admruls = [], []
    seen = set()

    def read(info: ET.Element) -> dict:
        d = {}
        for c in info:
            t = (c.text or "").strip()
            if t:
                d[c.tag] = t
        return d

    # 법률 / 시행령 / 시행규칙 — 태그 이름이 곧 단계입니다.
    for level in ("법률", "시행령", "시행규칙"):
        for node in root.findall(f".//{level}"):
            info = node.find("기본정보")
            if info is None:
                continue
            d = read(info)
            mst = pick(d, "법령일련번호")
            if not mst or mst in seen:
                continue
            seen.add(mst)
            laws.append({
                "level": level,
                "mst": mst,
                "id": pick(d, "법령ID"),
                "name": pick(d, "법령명"),
                "enforced": pick(d, "시행일자"),
                "promulgation_no": pick(d, "공포번호"),
            })

    # 위임행정규칙 — 어느 단계 아래에 붙어 있는지 함께 기록합니다.
    for level in ("법률", "시행령", "시행규칙"):
        for parent in root.findall(f".//{level}"):
            for ar in parent.findall("행정규칙"):
                for kind_node in ar:                    # <예규> / <고시> / <훈령> …
                    info = kind_node.find("기본정보")
                    if info is None:
                        continue
                    d = read(info)
                    rid = pick(d, "행정규칙일련번호")
                    if not rid or rid in seen:
                        continue
                    seen.add(rid)
                    admruls.append({
                        "level": level,                 # 어느 법령에 위임됐는지
                        "kind": kind_node.tag,          # 고시 / 예규 …
                        "id": rid,
                        "name": pick(d, "행정규칙명"),
                        "enforced": pick(d, "시행일자"),
                        "promulgation_no": pick(d, "발령번호"),
                    })

    return {"laws": laws, "admruls": admruls}


# =====================================================================
# 위임법령 조회 — 법률 조문이 어느 시행령·시행규칙 조문에 위임됐는지
# =====================================================================
# ★ 2026-08-18 — 실제 응답을 실측했습니다.
#   (/api/raw?target=lsDelegated&value=000160&mode=detail — 토양환경보전법)
#   구조가 **3단 중첩**입니다. 예전 파서는 `조문번호` 와 위임 대상 필드가
#   같은 엘리먼트의 직계 자식이라고 가정했는데, 실제로는 서로 다른 형제
#   가지에 들어 있어서 **항상 빈 리스트를 돌려주고 있었습니다.**
#
#   <lsDelegated><법령>
#     <법령정보>…</법령정보>
#     <위임조문정보>                       ← 법률 조문 1개당 1블록
#       <조정보>                           ← ★ 법률 쪽 조문번호는 여기
#         <조문번호>4</조문번호>
#         <조문가지번호>2</조문가지번호>    (가지조문일 때만 옵니다)
#         <조문제목>토양오염의 우려기준</조문제목>
#       </조정보>
#       <위임정보>                          ← 여러 개 반복
#         <위임구분>시행령</위임구분>
#         <위임법령일련번호>284891</위임법령일련번호>
#         <위임법령제목>토양환경보전법 시행령</위임법령제목>
#         <위임법령조문정보>                ← 여러 개 반복. ★ 대상 조문번호는 여기
#           <위임법령조문번호>6</위임법령조문번호>
#           <위임법령조문가지번호>2</위임법령조문가지번호>   (있을 때만)
#           <위임법령조문제목>…</위임법령조문제목>
#           <링크텍스트>대통령령</링크텍스트>
#           <라인텍스트>…</라인텍스트>
#           <조항호목>제12조제1항</조항호목>  ← 법률 쪽 몇 항/호에서 위임했는지
#         </위임법령조문정보>
#       </위임정보>
#       <위임정보>                          ← 위임행정규칙은 모양이 다릅니다
#         <위임구분>위임행정규칙</위임구분>
#         <위임행정규칙조문정보>            ← 제목·일련번호가 ★이 안에★ 있고
#           <위임행정규칙일련번호>2100000072350</위임행정규칙일련번호>
#           <위임행정규칙제목>토양지하수정보시스템 … 규정</위임행정규칙제목>
#           <링크텍스트>기후에너지환경부장관이 정한다</링크텍스트>
#           <조항호목>제4조의3제2항</조항호목>
#         </위임행정규칙조문정보>            ← 조문번호는 아예 안 옵니다
#       </위임정보>
#     </위임조문정보>
#   </법령></lsDelegated>
#
#   실측 분포(토양환경보전법 = 위임조문정보 55블록, 위임정보 102개):
#     인용법령 38 · 시행령 28 · 시행규칙 25 · 위임행정규칙 5 · 위임구분없음 6
#
#   ※ `위임구분`·제목·일련번호가 **통째로 없는** <위임정보> 가 6건 있습니다.
#     <위임법령조문정보> 만 덜렁 옵니다. 어느 법령의 조문인지 알 방법이
#     없으므로 **버립니다.** 바로 앞 형제의 제목을 물려받게 하면
#     "「토양환경보전법 시행규칙」 제4조의2" 처럼 실재하지 않는 인용을
#     만들어 내서, 안 붙이느니만 못합니다.
#
# 종류별 (제목 필드, 일련번호 필드, 조문번호 필드, 우리 라벨, 본문 조회 target)
DELEGATED_KINDS = (
    ("위임법령제목",     "위임법령일련번호",     "위임법령조문번호",     "법령",       "law"),
    ("위임행정규칙제목", "위임행정규칙일련번호", "위임행정규칙조문번호", "행정규칙",   "admrul"),
    ("위임자치법규제목", "위임자치법규일련번호", "위임자치법규조문번호", "자치법규",   "ordin"),
    ("위임규정제목",     "위임규정일련번호",     "위임규정조문번호",     "공공기관규정", ""),
    ("조약제목",         "조약일련번호",         "조약조문번호",         "조약",       ""),
)

# 위임구분별 정렬 우선순위(작을수록 먼저).
# `인용법령` 은 위임이 아니라 단순 상호참조인데 건수가 압도적입니다
# (실측 455건 중 354건). 호출부는 조문당 앞에서 몇 개만 잘라 쓰므로,
# 정렬을 안 해두면 정작 필요한 시행령·시행규칙이 밀려서 안 보입니다.
_DELE_RANK = {"시행령": 0, "시행규칙": 1, "위임행정규칙": 2,
              "위임자치법규": 3, "인용법령": 8}


def get_delegated(law_id: str) -> list[dict]:
    """
    target=lsDelegated. 법제처가 조문 단위로 "이 조문은 시행령 제O조에
    위임됨" 을 직접 알려줍니다. AI가 위임 조문번호를 추측/오인용하는 것을
    막는 데 씁니다 (예: 법 제53조제1항제2호 → 시행령 제72조제3항).

    파싱 대상 구조는 이 함수 바로 위 주석에 실측 결과를 그려 뒀습니다.
    <위임조문정보> 를 블록 단위로 돌면서 <조정보>(법률 쪽 조문번호) 와
    <위임정보>/*조문정보(위임 대상) 를 짝지어 평평한 레코드로 폅니다.

    중복 정리: 같은 "법 제N조 → 시행령 제M조" 가 항·호마다 반복해서 옵니다
    (예: 제12조제1항·제2항·제3항이 모두 시행령 제6조로). 힌트로는 한 번만
    있으면 되므로 (대상, 조문번호) 기준으로 합치고, 어느 항/호에서 왔는지는
    `_from` 리스트에 모아 둡니다.

    반환: 아래 키를 가진 dict 목록. (앞의 `_` 는 우리가 만든 정규화 필드)
        조문번호 / 조문가지번호 / 조문제목 : 법률 쪽(= 이 법령 자신의) 조문
        _kind    : "법령" / "행정규칙" / "자치법규" / "공공기관규정" / "조약"
        _kind_raw: 법제처 `위임구분` 원문 (시행령/시행규칙/인용법령/위임행정규칙)
        _target  : 본문 조회용 target ("law"/"admrul"/"ordin", 없으면 "")
        _title   : 위임 대상 법령·규칙 이름
        _seq     : 위임 대상 일련번호(MST)
        _jo      : 위임 대상 조문번호 — ★ 법령류만 옵니다. 행정규칙·자치법규는 "".
        _jo_gaji : 위임 대상 조문가지번호
        _jo_title: 위임 대상 조문제목
        _from    : ["제12조제1항", …] 이 위임이 걸린 법률 쪽 항·호 목록
        _rank    : 정렬 우선순위(작을수록 중요)
      실패하거나 결과가 없으면 빈 리스트.
    """
    try:
        xml_text = _get(SERVICE_URL, {"target": "lsDelegated", "ID": law_id})
    except LawApiError:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    merged: dict = {}
    order = 0

    for blk in root.iter("위임조문정보"):
        jo_info = blk.find("조정보")
        if jo_info is None:
            continue
        jo_no = (jo_info.findtext("조문번호") or "").strip()
        if not jo_no:
            continue
        jo_gaji = (jo_info.findtext("조문가지번호") or "").strip()
        jo_title = (jo_info.findtext("조문제목") or "").strip()

        for wi in blk.findall("위임정보"):
            # <위임정보> 직계의 스칼라 필드(위임구분·위임법령제목·일련번호).
            # 위임행정규칙은 이게 `위임구분` 하나뿐이고 나머지는 아래 조문정보
            # 안에 들어 있어서, 두 곳을 합쳐서 봅니다.
            head = {c.tag: (c.text or "").strip()
                    for c in wi if len(c) == 0 and (c.text or "").strip()}
            kind_raw = head.get("위임구분", "")

            for info in wi:
                if len(info) == 0:          # 스칼라 필드는 건너뜁니다
                    continue
                d = dict(head)
                d.update({c.tag: (c.text or "").strip()
                          for c in info if (c.text or "").strip()})

                title = seq = t_jo = t_gaji = t_title = ""
                kind_label = tgt = ""
                for t_key, s_key, j_key, label, tg in DELEGATED_KINDS:
                    if d.get(t_key):
                        title, seq = d[t_key], d.get(s_key, "")
                        t_jo = d.get(j_key, "")
                        t_gaji = d.get(j_key[:-2] + "가지번호", "")
                        t_title = d.get(j_key[:-2] + "제목", "")
                        kind_label, tgt = label, tg
                        break
                if not title:
                    # 제목이 없으면 어느 법령인지 특정할 수 없습니다.
                    # (실측 6건. 위 주석 참고 — 물려받게 하면 오히려 위험)
                    continue

                # ★ 조문번호가 `0` 으로 오는 경우가 있습니다(실측: 법 제12조 →
                #   「위험물안전관리법」 조문번호 0). "법률 전체를 가리킴" 이라는
                #   뜻이지 제0조가 아닙니다. 그대로 두면 "제0조" 라는 없는
                #   인용이 만들어지므로 빈 값으로 눕힙니다.
                if t_jo.lstrip("0") == "":
                    t_jo = t_gaji = ""

                key = (jo_no, jo_gaji, kind_label, title, t_jo, t_gaji)
                rec = merged.get(key)
                if rec is None:
                    rec = {
                        "조문번호": jo_no,
                        "조문가지번호": jo_gaji,
                        "조문제목": jo_title,
                        "위임구분": kind_raw or kind_label,
                        # ↓ 하위 호환용(예전 키 이름으로 읽는 코드가 있어도 동작)
                        "위임법령제목": title,
                        "위임법령조문번호": t_jo,
                        "위임법령조문가지번호": t_gaji,
                        "_kind": kind_label,
                        "_kind_raw": kind_raw or kind_label,
                        "_target": tgt,
                        "_title": title,
                        "_seq": seq,
                        "_jo": t_jo,
                        "_jo_gaji": t_gaji,
                        "_jo_title": t_title,
                        "_from": [],
                        "_rank": _DELE_RANK.get(kind_raw, 5),
                        "_order": order,
                    }
                    order += 1
                    merged[key] = rec
                src = d.get("조항호목", "")
                if src and src not in rec["_from"]:
                    rec["_from"].append(src)

    out = list(merged.values())
    out.sort(key=lambda r: (r["_rank"], r["_order"]))
    return out


def dele_key(jo_no, jo_gaji="") -> str:
    """delegation_map() 의 키. JSON 에 실려 나가므로 문자열이어야 합니다."""
    return f"{str(jo_no or '').strip()}|{str(jo_gaji or '').strip()}"


def delegation_map(law_id: str) -> dict:
    """
    get_delegated() 결과를 "조문번호|조문가지번호" → [위임 항목, …] 로 묶습니다.

    ★ 키는 반드시 **문자열**이어야 합니다. 이 dict 는 그대로 API 응답 JSON 에
      실려 나가는데, 파이썬 튜플 키는 json.dumps 가 못 씁니다
      ("TypeError: keys must be str, int, float, bool or None, not tuple").
      예전에는 파서가 항상 빈 dict 를 돌려줘서 이 문제가 드러나지 않았습니다.
      키를 만들 때는 dele_key() 를 쓰세요.
    실패해도 예외를 내지 않습니다 — 위임 정보는 보조 힌트이지 필수 데이터가
    아니므로, 이게 없다고 조문 조회 전체가 실패하면 안 됩니다.

    ★ 조문번호는 본문 조회에서 "53" 처럼 오고 lsDelegated 에서는 "0053" 처럼
      0 패딩으로 올 수 있습니다. 양쪽 키를 모두 넣어 두어 호출부가 어느
      형식으로 찾아도 걸리게 합니다. (가지번호도 마찬가지)

    각 리스트는 get_delegated() 가 매긴 _rank 순서를 그대로 유지합니다 —
    앞에서 몇 개만 잘라 써도 시행령·시행규칙이 먼저 잡힙니다.
    """
    try:
        rows = get_delegated(law_id)
    except Exception:
        return {}
    m: dict = {}
    for r in rows:
        no = r.get("조문번호", "")
        gaji = r.get("조문가지번호", "")
        nos = {no, no.lstrip("0") or no}
        gajis = {gaji, gaji.lstrip("0")} if gaji else {""}
        for n in nos:
            for g in gajis:
                m.setdefault(dele_key(n, g), []).append(r)
    return m


# =====================================================================
# JO 파라미터 — 법령 전체가 아니라 특정 조문 하나만 받기
# =====================================================================
def _jo_code(no, gaji="") -> str:
    """
    조문번호(+가지번호)를 JO 파라미터용 6자리 코드로 만듭니다.

    매뉴얼(현행법령 본문 조회):
        JO : 6자리숫자 = 조번호(4자리) + 조가지번호(2자리)
             000200 → 제2조,  001002 → 제10조의2

    입력 형식이 제각각이라 방어적으로 처리합니다.
        "2"      → 000200
        "10", 2  → 001002
        "0072"   → 007200      (0 패딩 4자리는 조번호로 봅니다)
        "007200" → 007200      (이미 6자리면 그대로)
        "제72조" → 007200      (숫자만 추려냅니다)
    빈 값이면 "" 를 돌려줍니다 — 호출부가 판단하게 둡니다.
    """
    raw = re.sub(r"\D", "", str(no or ""))
    if not raw:
        return ""
    if len(raw) >= 6:
        return raw[:6]
    if len(raw) == 5:
        return raw.zfill(6)          # 앞자리 0 이 유실된 경우
    g = re.sub(r"\D", "", str(gaji or "")) or "0"
    try:
        return f"{int(raw):04d}{int(g):02d}"
    except ValueError:
        return ""


def get_article(target: str, law_id: str = "", jo_no="", jo_gaji="",
                mst: str = "", ef_yd: str = "") -> dict:
    """
    법령의 **특정 조문 하나만** 받습니다. get_detail() 의 부분 조회판입니다.

    왜 필요한가:
      lsDelegated 가 "법 제53조 → 시행령 제72조" 를 조문번호까지 알려줍니다.
      그런데 그 시행령 제72조를 확인하려고 지금은 **시행령 전체**를 받습니다.
      큰 시행령은 수백 KB 라 AI 컨텍스트가 순식간에 찹니다.
      JO=007200 을 주면 제72조만 옵니다.

    법령 ID / MST 중 하나를 주세요 (매뉴얼: "ID 또는 MST 중 하나는 반드시 입력").
      · ID  로 조회하면 그 법령의 **현행** 본문이 옵니다. efYd 불필요.
      · MST 로 조회하면 그 **특정 판(버전)** 이 옵니다.
        ★ target=eflaw 는 MST 를 쓸 때 efYd(시행일자)가 **필수**입니다.

    반환: get_detail() 과 같은 {"meta":…, "articles":[…]} 모양.
          해당 조가 없으면 articles 가 빈 리스트입니다.
    """
    jo = _jo_code(jo_no, jo_gaji)
    if not jo:
        raise LawApiError(f"JO 코드를 만들 수 없습니다: 조문번호={jo_no!r} 가지={jo_gaji!r}")

    params = {"target": target, "JO": jo}
    if mst:
        params["MST"] = mst
        if target == "eflaw":
            if not ef_yd:
                raise LawApiError(
                    "target=eflaw 를 MST 로 조회할 때는 efYd(시행일자)가 필수입니다. "
                    "시행일자를 모르면 법령ID 로 조회하세요(현행 본문이 옵니다)."
                )
            params["efYd"] = str(ef_yd)
    elif law_id:
        params["ID"] = law_id
    else:
        raise LawApiError("법령ID 또는 MST 중 하나는 반드시 필요합니다.")

    xml_text = _get(SERVICE_URL, params)
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise LawApiError(f"XML 파싱 실패: {e}\n앞부분: {xml_text[:300]}") from e

    meta = {}
    basic = root.find(".//기본정보")
    if basic is not None:
        for c in basic:
            t = (c.text or "").strip()
            if t:
                meta[c.tag] = t
    return {"meta": meta, "articles": _extract_articles(root)}


def resolve_current_id(law_name: str, target: str = "") -> str:
    """
    법령명으로 **현행** 법령ID 를 찾습니다.

    lsDelegated 는 위임 대상의 `위임법령일련번호`(MST)만 주고 법령ID 는
    주지 않습니다. MST 로 target=eflaw 를 조회하려면 efYd 가 필요한데
    그 시행일자도 응답에 없습니다. 그래서 이름으로 현행판 ID 를 되찾습니다.

    정확히 같은 이름이 없으면 "" 를 돌려줍니다 — 부분 일치를 받아들이면
    시행령을 찾으려다 시행규칙을 물고 오는 사고가 납니다.
    """
    if not law_name:
        return ""
    tgt = target or LAW_TARGET
    try:
        rows = search(tgt, law_name, display=20)
    except LawApiError:
        return ""
    want = _norm_name(law_name)
    for r in rows:
        if _norm_name(row_name(tgt, r)) == want:
            return row_id(tgt, r)
    return ""


def get_delegated_articles(law_id: str, jo_no="", jo_gaji="",
                           max_fetch: int = 4) -> list[dict]:
    """
    법률 조문이 위임한 **하위법령 조문의 본문**까지 받아옵니다.

    3번(get_delegated) + 7번(JO) 을 잇는 함수입니다. 흐름:
      1) lsDelegated 로 "법 제53조 → 시행령 제72조" 를 얻습니다.
      2) 위임 대상 법령명으로 현행 법령ID 를 되찾습니다.
      3) 그 법령의 **제72조만** JO 로 받습니다. (전체를 받지 않습니다)

    jo_no 를 주면 그 조문의 위임만, 안 주면 법령 전체의 위임을 훑습니다.
    max_fetch — 본문까지 받아올 최대 건수. 호출 횟수 안전장치입니다.

    반환: [{"kind":…, "law":…, "jo":…, "jo_gaji":…, "articles":[…],
            "출처":"위임법령", "note":…}, …]
      위임 대상이 조문번호를 안 주는 종류(행정규칙·자치법규)는 articles 가
      비고 note 에 이유가 들어갑니다 — 그 경우는 이름만 힌트로 쓰세요.
    """
    rows = get_delegated(law_id)
    if not rows:
        return []

    if jo_no:
        want_no = re.sub(r"\D", "", str(jo_no)).lstrip("0")
        want_gaji = re.sub(r"\D", "", str(jo_gaji or ""))
        rows = [r for r in rows
                if r.get("조문번호", "").lstrip("0") == want_no
                and re.sub(r"\D", "", r.get("조문가지번호", "")) == want_gaji]

    out: list[dict] = []
    fetched = 0
    seen: set = set()

    for r in rows:
        title, tgt, jo = r["_title"], r["_target"], r["_jo"]
        key = (title, jo, r["_jo_gaji"])
        if not title or key in seen:
            continue
        seen.add(key)

        item = {
            "kind": r["_kind"], "law": title,
            "jo": jo, "jo_gaji": r["_jo_gaji"],
            "위임구분": r.get("위임구분", ""),
            "출처": "위임법령", "articles": [], "note": "",
        }

        # 조문번호가 오는 것은 법령(시행령·시행규칙·인용법령)뿐입니다.
        if not (tgt == "law" and jo):
            item["note"] = (f"{r['_kind']} 위임 — 법제처가 조문번호를 주지 않습니다. "
                            f"이름만 근거로 쓰세요.")
            out.append(item)
            continue

        if fetched >= max_fetch:
            item["note"] = f"조회 상한({max_fetch}건) 초과로 본문을 받지 않았습니다."
            out.append(item)
            continue

        target_id = resolve_current_id(title)
        if not target_id:
            item["note"] = f"「{title}」 의 현행 법령ID 를 찾지 못했습니다."
            out.append(item)
            continue

        try:
            got = get_article(LAW_TARGET, law_id=target_id,
                              jo_no=jo, jo_gaji=r["_jo_gaji"])
        except LawApiError as e:
            item["note"] = f"조문 조회 실패: {e}"
            out.append(item)
            continue

        fetched += 1
        item["articles"] = got["articles"]
        item["law_id"] = target_id
        if not got["articles"]:
            item["note"] = f"제{jo}조에 해당하는 조문이 응답에 없습니다."
        out.append(item)

    return out


# =====================================================================
# 법률명 약칭 조회 — "영"/"규칙" 축약 말고, 공식 약칭(예: 개인정보법 등)
# =====================================================================
# query 파라미터가 없는 API라 전체를 페이지로 받아야 합니다.
# 프로세스 수명 동안 한 번만 받고 캐시합니다 (자주 안 바뀌는 데이터).
_ABBR_CACHE: dict = {"data": None}


def get_abbreviations(max_pages: int = 30) -> dict:
    """
    target=lsAbrv. {정규화된 약칭 → 정식 법령명한글} 을 돌려줍니다.

    max_pages — 안전장치. display=100 기준 최대 max_pages*100 건까지만
      받습니다(기본 3,000건). 마지막 페이지 판정은 "받은 개수 < 100" 입니다.
    """
    if _ABBR_CACHE["data"] is not None:
        return _ABBR_CACHE["data"]

    mapping: dict = {}
    for page in range(1, max_pages + 1):
        try:
            xml_text = _get(SEARCH_URL, {"target": "lsAbrv", "display": 100, "page": page})
        except LawApiError:
            break
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            break
        rows = [c for c in root if len(c) > 0]
        if not rows:
            break
        # ★ lsAbrv 는 요청변수 목록에 `page` 가 없습니다(매뉴얼 확인).
        #   서버가 page 를 무시하면 1페이지를 30번 받게 되므로, 새로 들어온
        #   항목이 하나도 없으면 거기서 끊습니다. page 를 지원하면 이 조건은
        #   걸리지 않으니 양쪽 다 안전합니다.
        before = len(mapping)
        for row in rows:
            d = _elem_to_dict(row)
            # ★ 2026-08-19 — 목록 응답의 법령명 태그는 밑줄 있는 "법령명_한글" 이
            #   1순위입니다(NAME_KEYS 참고). 여기만 밑줄 없는 하나에 폴백 없이
            #   걸려 있어서, 태그가 다르면 표가 통째로 비고 약칭 복구 단계
            #   전체가 조용한 no-op 이 됩니다. 실측 못 한 필드라 넓게 받습니다.
            full = pick(d, "법령명_한글", "법령명한글", "법령명")
            abbr = pick(d, "법령약칭명", "약칭명", "법령약칭")
            if full and abbr:
                mapping[_norm_name(abbr)] = full
        if len(mapping) == before:
            break
        if len(rows) < 100:
            break

    # ★ 빈 표는 캐시하지 않습니다. 한 번의 일시적 실패가 프로세스 수명 내내
    #   약칭 복구를 죽여 버립니다(재시작 전까지 복구 불가).
    if not mapping:
        return mapping
    _ABBR_CACHE["data"] = mapping
    return mapping


def resolve_abbrev(name: str) -> str:
    """
    name 이 공식 약칭이면 정식 법령명으로 바꿔 돌려줍니다.
    약칭이 아니거나 조회에 실패하면 입력을 그대로 돌려줍니다 —
    호출부에서 실패를 따로 처리할 필요 없이 항상 안전하게 쓸 수 있습니다.
    """
    try:
        table = get_abbreviations()
    except Exception:
        return name
    return table.get(_norm_name(name), name)
