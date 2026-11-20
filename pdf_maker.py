"""
조회 결과를 PDF 문서로 만듭니다.

브라우저 인쇄를 쓰지 않는 이유:
  사용자가 인쇄 대화상자에서 "대상"을 직접 "PDF로 저장" 으로 바꿔야 하는데,
  기본값인 "Microsoft Print to PDF" 로 저장하면 화면이 이미지로 렌더링되어
  텍스트 복사·검색이 되지 않습니다. 그 선택을 사용자에게 맡길 수 없어
  서버에서 직접 만듭니다.
"""

import io
import os
import re
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

_FONT_CANDIDATES = [
    ("MalgunGothic", r"C:\Windows\Fonts\malgun.ttf",
     r"C:\Windows\Fonts\malgunbd.ttf"),
    ("NanumGothic", "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
     "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf"),
    ("NanumBarunGothic", "/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf",
     "/usr/share/fonts/truetype/nanum/NanumBarunGothicBold.ttf"),
    ("NotoSansKR", "/usr/share/fonts/truetype/noto/NotoSansKR-Regular.ttf",
     "/usr/share/fonts/truetype/noto/NotoSansKR-Bold.ttf"),
    ("AppleGothic", "/Library/Fonts/AppleGothic.ttf", ""),
    # 참고: NotoSansCJK-*.ttc 는 CFF(포스트스크립트) 윤곽선이라
    #       reportlab 이 읽지 못합니다. 후보에서 제외했습니다.
]

# NotoSansCJK 는 SC/TC/JP/KR 이 한 파일에 들어 있어 번호로 골라야 합니다.
_TTC_KR_INDEX = 2

FONT = None
FONT_BOLD = None


def _setup_font():
    """쓸 수 있는 한글 폰트를 하나 골라 등록합니다."""
    global FONT, FONT_BOLD
    if FONT:
        return FONT, FONT_BOLD

    for name, regular, bold in _FONT_CANDIDATES:
        if not regular or not os.path.exists(regular):
            continue
        try:
            if regular.lower().endswith(".ttc"):
                # TTC 는 subfontIndex 로 한국어 글꼴을 지정해야 합니다.
                pdfmetrics.registerFont(TTFont(name, regular, subfontIndex=_TTC_KR_INDEX))
            else:
                pdfmetrics.registerFont(TTFont(name, regular))
            FONT = name

            if bold and os.path.exists(bold):
                bname = name + "-Bold"
                if bold.lower().endswith(".ttc"):
                    pdfmetrics.registerFont(
                        TTFont(bname, bold, subfontIndex=_TTC_KR_INDEX))
                else:
                    pdfmetrics.registerFont(TTFont(bname, bold))
                FONT_BOLD = bname
            else:
                FONT_BOLD = name
            return FONT, FONT_BOLD
        except Exception:
            FONT = FONT_BOLD = None
            continue

    raise RuntimeError(
        "PDF 에 쓸 한글 폰트를 찾지 못했습니다.\n"
        "  · 라즈베리파이/리눅스 :  sudo apt install fonts-nanum\n"
        "  · 윈도우             :  맑은 고딕(malgun.ttf)이 있어야 합니다\n"
        "(NotoSansCJK 의 .ttc 파일은 형식이 달라 사용할 수 없습니다)"
    )


def _styles():
    f, fb = _setup_font()
    ink, note, accent, seal = "#1a1c20", "#6f6d64", "#2c4a52", "#a8332c"
    return {
        "title": ParagraphStyle("t", fontName=fb, fontSize=15, leading=21,
                                textColor=colors.HexColor(ink), spaceAfter=2),
        "meta": ParagraphStyle("m", fontName=f, fontSize=8.5, leading=13,
                               textColor=colors.HexColor(note)),
        "q": ParagraphStyle("q", fontName=fb, fontSize=12, leading=18,
                            textColor=colors.HexColor(ink), spaceBefore=4, spaceAfter=3),
        "cond": ParagraphStyle("c", fontName=f, fontSize=9, leading=14,
                               textColor=colors.HexColor(accent), spaceAfter=6),
        "h2": ParagraphStyle("h2", fontName=fb, fontSize=10.5, leading=15,
                             textColor=colors.HexColor(accent),
                             spaceBefore=13, spaceAfter=5),
        "citeh": ParagraphStyle("ch", fontName=fb, fontSize=10, leading=15,
                                textColor=colors.HexColor(ink), spaceBefore=7),
        "citen": ParagraphStyle("cn", fontName=f, fontSize=8.5, leading=12,
                                textColor=colors.HexColor(note), spaceAfter=3),
        "body": ParagraphStyle("b", fontName=f, fontSize=9.5, leading=15.5,
                               textColor=colors.HexColor(ink), alignment=TA_LEFT),
        "art": ParagraphStyle("a", fontName=f, fontSize=9, leading=15,
                              textColor=colors.HexColor(ink),
                              leftIndent=8, spaceAfter=2),
        "warn": ParagraphStyle("w", fontName=f, fontSize=8.5, leading=13,
                               textColor=colors.HexColor(seal),
                               borderPadding=4, spaceBefore=10),
    }


_ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}


def _esc(s: str) -> str:
    return "".join(_ESC.get(c, c) for c in str(s or ""))


def _rich(text: str, fb: str) -> str:
    """답변의 **굵게** __밑줄__ !!중요!! 표시를 PDF 서식으로 바꿉니다."""
    t = _esc(text)
    t = re.sub(r"!!(.+?)!!", r'<b><font color="#8a6a00">\1</font></b>', t)
    t = re.sub(r"\*\*(.+?)\*\*", rf'<font name="{fb}" color="#2c4a52">\1</font>', t)
    t = re.sub(r"__(.+?)__", r"<u>\1</u>", t)
    return t


def _split_hang(text: str) -> list[str]:
    """조문 본문을 항(①②③)·호(1. 2.) 단위로 나눕니다."""
    if not text:
        return []
    t = re.sub(r"^제\d+조(의\d+)?\s*\([^)]*\)\s*", "", str(text))
    t = re.sub(r"\s*([①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮])\s*", r"\n\1 ", t)
    t = re.sub(r"(^|[가-힣\s.>”\"』」\)])(\d{1,2})\.\s*(?=[가-힣“\"'(「『])",
               lambda m: ("" if m.group(1) == "\n" else m.group(1)) + f"\n{m.group(2)}. ", t)
    t = re.sub(r"(^|[\s.>”\"』」\)])([가나다라마바사아자차카타파하])\.\s*(?=[가-힣“\"'(「『])",
               lambda m: ("" if m.group(1) == "\n" else m.group(1)) + f"\n   {m.group(2)}. ", t)
    return [ln.strip() for ln in t.split("\n") if ln.strip()]


_BP_TITLE = re.compile(r"^\[(별표|별지|서식)\s*(\d+(?:의\d+)?)\]")


def cite_key(law: dict, a: dict) -> str:
    """인용 열쇠 — 조문 "법령명|조문번호|가지번호", 별표 "법령명|별표 N" (화면 index.html 의 citeKey 와 같은 규칙)."""
    if a.get("조문번호"):
        return f'{law.get("name", "")}|{a["조문번호"]}|{a.get("조문가지번호") or ""}'
    m = _BP_TITLE.match(str(a.get("조문제목") or ""))
    return f'{law.get("name", "")}|{m.group(1)} {m.group(2)}' if m else ""


def build(payload: dict) -> bytes:
    """
    조회 결과를 PDF 바이트로 만듭니다.

    payload: {question, answered, answer, citations[], laws[], generated_at}
    """
    st = _styles()
    fb = FONT_BOLD

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=17 * mm, rightMargin=15 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title="법령 조회 결과", author="법령 조회 도우미",
    )

    story = []
    now = payload.get("generated_at") or datetime.now().strftime("%Y-%m-%d %H:%M")

    story.append(Paragraph("법령 조회 결과", st["title"]))
    story.append(Paragraph(
        f"국가법령정보 OPEN API · {_esc(now)} 조회", st["meta"]))
    story.append(Spacer(1, 3 * mm))
    story.append(HRFlowable(width="100%", thickness=1.1,
                            color=colors.HexColor("#1a1c20"), spaceAfter=6))

    if payload.get("question"):
        story.append(Paragraph(_esc(payload["question"]), st["q"]))
    if payload.get("answered"):
        story.append(Paragraph("확인된 조건 — " + _esc(payload["answered"]), st["cond"]))

    cites = payload.get("citations") or []
    if cites:
        story.append(Paragraph("근거 조문", st["h2"]))
        for c in cites:
            head = f'{_esc(c.get("label", ""))}'
            law = _esc(c.get("law", "")) or "확인 안 됨"
            lvl = _esc(c.get("level", ""))
            block = [
                Paragraph(f'<font name="{fb}">{head}</font>'
                          f'  <font size="8.5" color="#6f6d64">{law}'
                          f'{" · " + lvl if lvl else ""}</font>', st["citeh"]),
                Paragraph(_esc(c.get("title", "") or "제목 없음"), st["citen"]),
            ]
            for ln in _split_hang(c.get("text", "")):
                block.append(Paragraph(_esc(ln), st["art"]))
            if c.get("mismatch"):
                block.append(Paragraph("⚠ " + _esc(c["mismatch"]), st["warn"]))
            story.append(KeepTogether(block))

    if payload.get("answer"):
        story.append(Paragraph("답변", st["h2"]))
        for para in str(payload["answer"]).split("\n"):
            p = para.strip()
            if not p:
                story.append(Spacer(1, 2 * mm))
                continue
            m = re.match(r"^【(.+?)】\s*$", p)
            if m:
                story.append(Paragraph(_esc(m.group(1)), st["h2"]))
            else:
                story.append(Paragraph(_rich(p, fb), st["body"]))

    laws = payload.get("laws") or []
    keys = set(payload.get("cited_keys") or [])
    if keys:
        # ★ v1.64 — 답변이 인용한 조문·별표만 싣습니다. 예전에는 검색한 법령의 조문 전부(한 답에 300~750개)를 실어
        #   7개 법령 답변 하나가 121쪽이었습니다. 화면도 인용된 것만 펼쳐 보여줍니다.
        laws = [{**l, "articles": [a for a in (l.get("articles") or []) if cite_key(l, a) in keys]} for l in laws]
    shown = [l for l in laws if l.get("articles")]
    if shown:
        story.append(PageBreak())
        story.append(Paragraph("조문 원문", st["h2"]))
        for l in shown:
            head = _esc(l.get("name", ""))
            lvl = _esc(l.get("level") or l.get("kind") or "")
            ef = _esc(l.get("enforced", ""))
            story.append(Paragraph(
                f'<font name="{fb}">{head}</font>'
                f'  <font size="8.5" color="#6f6d64">{lvl}'
                f'{" · 시행 " + ef if ef else ""}</font>', st["citeh"]))
            for a in l["articles"]:
                if a.get("조문여부") == "전문":
                    continue
                no, gaji = a.get("조문번호", ""), a.get("조문가지번호", "")
                title = a.get("조문제목", "")
                label = (f"제{no}조" + (f"의{gaji}" if gaji else "")
                         + (f"({title})" if title else "")) if no else title
                if label:
                    story.append(Paragraph(f'<font name="{fb}">{_esc(label)}</font>',
                                           st["citen"]))
                for ln in _split_hang(a.get("조문내용", "")):
                    story.append(Paragraph(_esc(ln), st["art"]))
            story.append(Spacer(1, 3 * mm))

    story.append(Spacer(1, 6 * mm))
    story.append(HRFlowable(width="100%", thickness=0.5,
                            color=colors.HexColor("#d6d3c9"), spaceAfter=5))
    story.append(Paragraph(
        "이 문서는 AI 가 정리한 참고 자료입니다. 조·항·호가 잘못 인용될 수 있으므로 "
        "반드시 조문 원문과 대조하세요. 법령의 효력 기준은 관보입니다.", st["warn"]))

    def _footer(canvas, d):
        canvas.saveState()
        canvas.setFont(FONT, 8)
        canvas.setFillColor(colors.HexColor("#6f6d64"))
        canvas.drawRightString(A4[0] - 15 * mm, 10 * mm, f"{canvas.getPageNumber()}")
        canvas.drawString(17 * mm, 10 * mm, "법령 조회 도우미")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()
