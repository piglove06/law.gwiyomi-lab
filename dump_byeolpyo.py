# -*- coding: utf-8 -*-
"""
6번 실측 — 법령 본문 조회(lawService)에 별표가 정말 딸려오는지 확인합니다.

배경:
  매뉴얼 '현행법령(공포일/시행일) 본문 조회' 응답 필드 목록(3/3쪽)에는
  별표번호 / 별표가지번호 / 별표구분 / 별표제목 / 별표내용 / 별표서식PDF파일링크
  가 전부 적혀 있습니다.
  그런데 law_client.py:59-63 과 main.py:1121 은 "법령은 본문에 별표가
  아예 안 딸려온다"고 단정하고 get_byeolpyo() 우회 경로를 만들어 뒀습니다.
  어느 쪽이 맞는지 원본 XML 로 확정합니다.

같이 확인하는 것:
  · <별표단위> 가 실제로 잡히는지 (law_client.py:403 의 XPath 가 맞는지)
  · 별표번호 원본 값이 '0004' 인지 '000400' 인지 '4' 인지
    → law_client.py:418 의 lstrip("0") 이 안전한지 판정
  · 별표가지번호가 따로 오는지 (별표 6 / 별표 6의3 구분 가능한지)
  · 별표내용에 표 본문 텍스트가 있는지, 파일 링크만 있는지

실행:
  cd "C:\\Users\\USER\\OneDrive\\Desktop\\이정원\\26.08.07 LAW OPEN DATA\\files"
  python dump_byeolpyo.py
  python dump_byeolpyo.py "물환경보전법 시행규칙"     # 다른 법령으로 보고 싶을 때

표준 라이브러리만 씁니다. .venv 활성화 없이 그냥 python 으로 돌아갑니다.
결과는 files\_dump\ 폴더에 저장됩니다.
"""

import os
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

SEARCH_URL = "http://www.law.go.kr/DRF/lawSearch.do"
SERVICE_URL = "http://www.law.go.kr/DRF/lawService.do"
TIMEOUT = 30

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "_dump")

# 별표 4(토양오염검사주기)를 인용하는 법령이라 확인용으로 딱 맞습니다.
DEFAULT_LAW = "토양환경보전법 시행규칙"


def read_oc() -> str:
    """.env 에서 LAW_OC 를 읽습니다. 환경변수가 있으면 그쪽을 먼저 씁니다."""
    oc = os.getenv("LAW_OC", "").strip()
    if oc:
        return oc
    env_path = os.path.join(HERE, ".env")
    if not os.path.exists(env_path):
        sys.exit(f"[중단] .env 를 찾을 수 없습니다: {env_path}")
    for enc in ("utf-8-sig", "utf-8", "cp949"):
        try:
            with open(env_path, encoding=enc) as fp:
                lines = fp.readlines()
            break
        except UnicodeDecodeError:
            continue
    else:
        sys.exit("[중단] .env 인코딩을 읽을 수 없습니다.")
    for line in lines:
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() == "LAW_OC":
            return v.strip().strip('"').strip("'")
    sys.exit("[중단] .env 에 LAW_OC 가 없습니다.")


def get(url: str, params: dict) -> str:
    full = url + "?" + urllib.parse.urlencode(params, encoding="utf-8")
    req = urllib.request.Request(full, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
    for enc in ("utf-8", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def save(name: str, text: str) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(text)
    return path


def find_law(oc: str, name: str) -> dict:
    """eflaw 목록 조회로 법령ID / 법령일련번호를 얻습니다."""
    xml_text = get(SEARCH_URL, {
        "OC": oc, "target": "eflaw", "type": "XML",
        "query": name, "display": 20, "nw": "3",
    })
    save("00_search_eflaw.xml", xml_text)
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        sys.exit("[중단] 목록 조회 XML 파싱 실패. _dump\\00_search_eflaw.xml 을 열어보세요.\n"
                 f"앞부분: {xml_text[:300]}")

    want = name.replace(" ", "")
    rows = []
    for child in root:
        if len(child) == 0:
            continue
        d = {c.tag: (c.text or "").strip() for c in child}
        rows.append(d)

    if not rows:
        sys.exit(f"[중단] '{name}' 검색 결과 0건.")

    # 법령명이 정확히 일치하는 것을 우선합니다.
    for d in rows:
        nm = (d.get("법령명한글") or d.get("법령명_한글") or "").replace(" ", "")
        if nm == want:
            return d
    print(f"[알림] 정확히 일치하는 법령명이 없어 첫 결과를 씁니다: "
          f"{rows[0].get('법령명한글', '?')}")
    return rows[0]


def report(tag_label: str, xml_text: str, fname: str) -> None:
    path = save(fname, xml_text)
    print(f"\n{'=' * 68}")
    print(f"[{tag_label}]  저장: {path}  ({len(xml_text):,} 자)")
    print("=" * 68)

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"  XML 파싱 실패: {e}")
        print(f"  앞부분: {xml_text[:300]}")
        return

    # --- 루트 바로 아래 구조 -------------------------------------
    print("  루트 <%s> 직계 자식: %s" % (
        root.tag,
        ", ".join(f"<{c.tag}>({len(c)})" for c in root) or "(없음)",
    ))

    # --- 별표 관련 태그가 어디에 어떤 경로로 있는지 --------------
    def walk(node, path_parts):
        for c in node:
            cur = path_parts + [c.tag]
            if "별표" in c.tag:
                yield "/".join(cur), c
            else:
                yield from walk(c, cur)

    hits = list(walk(root, [root.tag]))
    if not hits:
        print("  >>> 별표 관련 태그 없음. 매뉴얼과 달리 별표가 안 딸려옵니다.")
        print("      → law_client.py:59-63 주석이 맞음. get_byeolpyo() 우회 유지 필요.")
        return

    # 경로별로 몇 개인지
    counts = {}
    for p, _ in hits:
        counts[p] = counts.get(p, 0) + 1
    print("  >>> 별표 관련 태그 발견:")
    for p, n in sorted(counts.items()):
        print(f"        {p}  x{n}")

    # --- law_client.py 가 쓰는 XPath 가 맞는지 -------------------
    units = root.findall(".//별표단위")
    print(f"\n  law_client.py:403 의 XPath './/별표단위' → {len(units)} 건")
    if not units:
        print("      >>> XPath 불일치! 위 경로를 보고 findall 을 고쳐야 합니다.")

    # --- 실제 값 확인 (앞 8건) ----------------------------------
    targets = units or [c for _, c in hits if len(c) > 0]
    if not targets:
        return
    print(f"\n  샘플 {min(8, len(targets))}건 원본 값:")
    print(f"  {'별표번호':>10} {'가지번호':>8} {'구분':>6}  {'내용길이':>8}  제목")
    print(f"  {'-' * 66}")
    for u in targets[:8]:
        def g(tag):
            el = u.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        body_el = u.find("별표내용")
        body = ""
        if body_el is not None:
            body = " ".join(t.strip() for t in body_el.itertext() if t and t.strip())
        print("  %10r %8r %6s  %8d  %s" % (
            g("별표번호"), g("별표가지번호"), g("별표구분") or "-",
            len(body), (g("별표제목") or "(제목없음)")[:34],
        ))

    # --- 판정 --------------------------------------------------
    nos = []
    for u in targets:
        el = u.find("별표번호")
        v = (el.text or "").strip() if el is not None and el.text else ""
        if v:
            nos.append(v)
    if nos:
        lens = sorted({len(v) for v in nos})
        print(f"\n  별표번호 자릿수: {lens}")
        if 6 in lens:
            print("      >>> 6자리(000400 꼴). law_client.py:418 의 lstrip('0') 은")
            print("          000400 → '400' 이 되어 버립니다. _byl_no() 로 교체 필요.")
        elif 4 in lens:
            print("      >>> 4자리(0004 꼴). lstrip('0') 은 '4' 로 맞습니다.")
            print("          단 별표가지번호를 따로 읽지 않는 문제는 그대로입니다.")
        else:
            print("      >>> 예상 밖 형식. 위 샘플 값을 보고 판단하세요.")

    gajis = []
    for u in targets:
        el = u.find("별표가지번호")
        v = (el.text or "").strip() if el is not None and el.text else ""
        if v and v.strip("0"):
            gajis.append(v)
    print(f"  별표가지번호가 0 이 아닌 항목: {len(gajis)}건 {gajis[:6]}")

    bodies = 0
    for u in targets:
        el = u.find("별표내용")
        if el is not None:
            t = " ".join(x.strip() for x in el.itertext() if x and x.strip())
            if len(t) > 50:
                bodies += 1
    print(f"  별표내용에 50자 넘는 본문이 있는 항목: {bodies} / {len(targets)}건")
    if bodies == 0:
        print("      >>> 표 본문은 안 옵니다. 파일 링크만 확보 가능.")
    else:
        print("      >>> 표 본문이 옵니다! get_byeolpyo() 우회 없이 본문에서 바로 쓸 수 있습니다.")


def main() -> None:
    law_name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LAW
    oc = read_oc()
    print(f"LAW_OC = {oc[:3]}*** / 대상 법령 = {law_name}")

    row = find_law(oc, law_name)
    law_id = row.get("법령ID", "")
    mst = row.get("법령일련번호", "")
    print(f"\n찾음: {row.get('법령명한글', '?')}"
          f"  법령ID={law_id}  법령일련번호(MST)={mst}"
          f"  시행일자={row.get('시행일자', '?')}"
          f"  현행연혁코드={row.get('현행연혁코드', '?')}")

    if not law_id:
        sys.exit("[중단] 법령ID 를 못 얻었습니다. _dump\\00_search_eflaw.xml 확인하세요.")

    # 시행일 기준 — 지금 코드가 기본으로 쓰는 경로입니다.
    report("target=eflaw  (ID=%s)" % law_id,
           get(SERVICE_URL, {"OC": oc, "target": "eflaw", "type": "XML", "ID": law_id}),
           "01_eflaw_detail.xml")

    # 공포일 기준 — 되돌리기 경로.
    report("target=law  (ID=%s)" % law_id,
           get(SERVICE_URL, {"OC": oc, "target": "law", "type": "XML", "ID": law_id}),
           "02_law_detail.xml")

    # 참고: 별표·서식 목록 조회(licbyl). get_byeolpyo() 가 쓰는 경로입니다.
    # 매뉴얼 PDF 가 없어 필드명이 전부 실측 추측이라 원본을 같이 떠 둡니다.
    try:
        report("target=licbyl  (query=%s, search=2)" % law_name,
               get(SEARCH_URL, {"OC": oc, "target": "licbyl", "type": "XML",
                                "query": law_name, "search": "2", "display": 100}),
               "03_licbyl_search.xml")
    except Exception as e:
        print(f"\n[알림] licbyl 조회 실패(권한 미신청일 수 있습니다): {e}")

    print(f"\n\n완료. '{OUT_DIR}' 폴더가 생겼습니다.")
    print("이 폴더째로 저(Claude)에게 알려주시면 원본 XML 을 직접 읽고")
    print("별표 로직을 확정해서 패치하겠습니다.")


if __name__ == "__main__":
    main()
