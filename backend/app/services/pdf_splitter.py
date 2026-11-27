"""Multi-page customer PDF splitter (SPEC_V1_5_CAM_SHOPFLOOR.md §2.5).

Deterministic first: per page the bottom-right title-block zone (bottom 25 % height, right 45 % width) is read from
the PDF text layer, a part / drawing number and a sheet indicator ("Sheet 1 of 2", "Blad 2/2") are matched with
configurable regexes, and pages are grouped into logical parts. Only pages whose title block has (almost) no text
layer (scans) go to one batched vision-model call that answers ``[{page_index, part_number, sheet_index,
is_continuation}]``.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MIN_TEXT_CHARS = 15
ZONE_H, ZONE_W = 0.25, 0.45

DEFAULT_PART_REGEXES = [
    # labelled fields (EN / NL / DE), value on the same line or the next one
    r"(?:part|drawing|dwg|article|item)\s*(?:no|nr|number|num|#)\.?\s*[:#]?\s*\n?\s*([A-Z0-9][A-Z0-9._/\-]{2,})",
    r"(?:teken(?:ing)?|artikel|onderdeel|werkstuk)\s*(?:nummer|nr|no)\.?\s*[:#]?\s*\n?\s*([A-Z0-9][A-Z0-9._/\-]{2,})",
    r"(?:zeichnungs|teile|sach)(?:nummer|nr)\.?\s*[:#]?\s*\n?\s*([A-Z0-9][A-Z0-9._/\-]{2,})",
]
FALLBACK_PART_REGEX = r"\b([A-Z]{2,6}[-_.]?\d{2,}(?:[-_.][A-Z0-9]{1,6})*)\b"
SHEET_REGEX = r"(?:sheet|blad|blatt|page|pagina)\s*:?\s*(\d{1,3})\s*(?:of|van|von|/|\\)\s*(\d{1,3})"
IGNORE_TOKENS = {"ISO", "DIN", "EN", "NEN", "ANSI", "ASME"}


@dataclass
class PageInfo:
    page_index: int
    text_chars: int
    part_number: str | None = None
    sheet_index: int | None = None
    sheet_count: int | None = None
    is_continuation: bool = False
    source: str = "text"  # text | llm | none


@dataclass
class PartGroup:
    part_number: str
    pages: list[int] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def title_block_text(page) -> str:
    """Text inside the bottom-right title-block zone of a PyMuPDF page (rotation-aware via the page rect)."""
    import pymupdf

    r = page.rect
    clip = pymupdf.Rect(r.x1 - r.width * ZONE_W, r.y1 - r.height * ZONE_H, r.x1, r.y1)
    return page.get_text("text", clip=clip) or ""


def parse_title_block(text: str, part_regexes: list[str] | None = None, sheet_regex: str = SHEET_REGEX
                      ) -> tuple[str | None, int | None, int | None]:
    """(part_number, sheet_index, sheet_count) from title-block text; regexes are case-insensitive."""
    part = None
    for rx in part_regexes or DEFAULT_PART_REGEXES:
        m = re.search(rx, text, re.IGNORECASE)
        if m:
            part = m.group(1).strip().rstrip(".-_/")
            break
    if part is None:
        for m in re.finditer(FALLBACK_PART_REGEX, text):
            tok = m.group(1)
            if re.split(r"[-_.]", tok)[0].upper() not in IGNORE_TOKENS:
                part = tok
                break
    sheet_i = sheet_n = None
    m = re.search(sheet_regex, text, re.IGNORECASE)
    if m:
        sheet_i, sheet_n = int(m.group(1)), int(m.group(2))
        if sheet_n < 1 or sheet_i < 1 or sheet_i > sheet_n:
            sheet_i = sheet_n = None
    return (part.upper() if part else None), sheet_i, sheet_n


def analyse_pages(pdf_path: Path, part_regexes: list[str] | None = None,
                  sheet_regex: str = SHEET_REGEX) -> list[PageInfo]:
    import pymupdf

    out = []
    with pymupdf.open(pdf_path) as doc:
        for i, page in enumerate(doc):
            text = title_block_text(page)
            chars = len(re.sub(r"\s", "", text))
            part, si, sn = parse_title_block(text, part_regexes, sheet_regex) if chars >= MIN_TEXT_CHARS else (None, None, None)
            out.append(PageInfo(i, chars, part, si, sn, bool(si and si > 1),
                                "text" if chars >= MIN_TEXT_CHARS else "none"))
    return out


def render_title_crops(pdf_path: Path, pages: list[int], out_dir: Path, dpi: int = 110) -> list[Path]:
    """Low-res PNG crops of the title-block zone (for the scanned-page fallback)."""
    import pymupdf

    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    with pymupdf.open(pdf_path) as doc:
        for i in pages:
            page = doc[i]
            r = page.rect
            clip = pymupdf.Rect(r.x1 - r.width * ZONE_W, r.y1 - r.height * ZONE_H, r.x1, r.y1)
            p = out_dir / f"title_p{i + 1:03d}.png"
            page.get_pixmap(dpi=dpi, clip=clip).save(p)
            paths.append(p)
    return paths


def apply_llm_answers(infos: list[PageInfo], answers: list[dict]) -> None:
    by_page = {int(a["page_index"]): a for a in answers if isinstance(a, dict) and "page_index" in a}
    for info in infos:
        a = by_page.get(info.page_index)
        if info.source != "none" or not a:
            continue
        pn = (a.get("part_number") or "").strip().upper() or None
        info.part_number = pn
        info.sheet_index = int(a["sheet_index"]) if a.get("sheet_index") else None
        info.is_continuation = bool(a.get("is_continuation"))
        info.source = "llm"


def group_pages(infos: list[PageInfo]) -> list[PartGroup]:
    """Pages sharing a part number form one part (in first-seen order); pages without a number continue the
    previous part when they are a continuation sheet (or carry no information at all), else start an anonymous part."""
    groups: list[PartGroup] = []
    by_number: dict[str, PartGroup] = {}
    for info in infos:
        g = None
        if info.part_number:
            g = by_number.get(info.part_number)
            if g is None:
                g = by_number[info.part_number] = PartGroup(info.part_number)
                groups.append(g)
        elif groups and (info.is_continuation or info.sheet_index is None):
            g = groups[-1]
        if g is None:
            g = PartGroup(f"PAGE-{info.page_index + 1}")
            groups.append(g)
        g.pages.append(info.page_index)
        g.sources.append(info.source)
    return groups


def write_group_pdf(src: Path, pages: list[int], dest: Path) -> Path:
    import pymupdf

    dest.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(src) as doc, pymupdf.open() as out:
        for p in pages:
            out.insert_pdf(doc, from_page=p, to_page=p)
        out.save(dest, garbage=3, deflate=True)
    return dest


def safe_name(part_number: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", part_number).strip("._") or "part"
