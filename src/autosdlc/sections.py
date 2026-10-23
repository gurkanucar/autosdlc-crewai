"""Markdown belgeleri başlık ve gereksinim (FR/NFR) düzeyinde bölümlere ayırır ve bölüm düzenlemelerini uygular."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass


HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
REQUIREMENT_LINE = re.compile(r"^\s*(?:[-*+]\s+|\|\s*|\d+\.\s+)?\**\s*(N?FR-\d{1,3})\b")


@dataclass(frozen=True)
class Section:
    key: str          # Kararlı kimlik: "h:<sıra>" başlıklar, "r:FR-01" gereksinim satırları
    title: str
    kind: str         # "heading" | "requirement"
    level: int        # başlık seviyesi; gereksinimlerde içinde bulunduğu başlığın seviyesi + 1
    start: int        # ilk satırın indeksi
    end: int          # son satırdan sonraki indeks

    def text(self, lines: list[str]) -> str:
        return "\n".join(lines[self.start:self.end])


def _code_fence_mask(lines: list[str]) -> list[bool]:
    inside, mask = False, []
    for line in lines:
        if line.lstrip().startswith("```"):
            mask.append(True)
            inside = not inside
        else:
            mask.append(inside)
    return mask


def split_sections(markdown: str) -> list[Section]:
    """Başlıklar (bir sonraki eşit/üst başlığa kadar) ve FR/NFR satırları (paragraf sonuna kadar)."""
    lines = markdown.split("\n")
    fenced = _code_fence_mask(lines)
    headings = [(index, len(match.group(1)), match.group(2).strip())
                for index, line in enumerate(lines) if not fenced[index] and (match := HEADING.match(line))]
    sections: list[Section] = []
    for number, (index, level, title) in enumerate(headings):
        end = next((other for other, other_level, _ in headings if other > index and other_level <= level), len(lines))
        sections.append(Section(f"h:{number}", title, "heading", level, index, end))
    seen: set[str] = set()
    for index, line in enumerate(lines):
        if fenced[index] or HEADING.match(line):
            continue
        match = REQUIREMENT_LINE.match(line)
        if not match or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        end = index + 1
        # Tablo satırı tek satırdır; madde/paragraf ise boş satıra, başlığa ya da sonraki gereksinime kadar sürer.
        if not line.lstrip().startswith("|"):
            while end < len(lines) and lines[end].strip() and not HEADING.match(lines[end]) and not REQUIREMENT_LINE.match(lines[end]):
                end += 1
        parent = max((level for start, level, _ in headings if start < index), default=0)
        summary = re.sub(r"[|*`]", " ", line).strip()
        sections.append(Section(f"r:{match.group(1)}", re.sub(r"\s+", " ", summary)[:90], "requirement", parent + 1, index, end))
    return sorted(sections, key=lambda section: (section.start, section.kind != "heading"))


def find_section(markdown: str, key: str) -> Section | None:
    return next((section for section in split_sections(markdown) if section.key == key), None)


def replace_section(markdown: str, section: Section, new_text: str) -> str:
    lines = markdown.split("\n")
    replacement = new_text.strip("\n").split("\n") if new_text.strip() else []
    return "\n".join(lines[:section.start] + replacement + lines[section.end:])


def insert_after(markdown: str, section: Section, new_text: str) -> str:
    lines = markdown.split("\n")
    addition = [""] + new_text.strip("\n").split("\n") + ([""] if section.end < len(lines) and lines[section.end].strip() else [])
    return "\n".join(lines[:section.end] + addition + lines[section.end:])


def delete_section(markdown: str, section: Section) -> str:
    return replace_section(markdown, section, "")


def unified_diff(before: str, after: str, name: str) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"{name} (önce)", tofile=f"{name} (sonra)", n=2,
    ))


def strip_fences(text: str) -> str:
    """Model yanıtı ```markdown … ``` içine sarılmışsa çıkarır."""
    stripped = text.strip()
    match = re.fullmatch(r"```[\w-]*\n(.*?)\n?```", stripped, re.DOTALL)
    return match.group(1) if match else stripped


NO_CHANGE = "DEĞİŞİKLİK YOK"
AFTER_HINT = re.compile(r"^\s*<!--\s*SONRA:\s*(.+?)\s*-->\s*$")
CHANGE_TAG = re.compile(r"\s*\[(?:CR|cr)-\d+\]")


def _normalize(title: str) -> str:
    return re.sub(r"\s+", " ", CHANGE_TAG.sub("", title)).strip(" *`").casefold()


def _patch_chunks(patch: str) -> list[tuple[str, int, str, str | None]]:
    """Yamayı (başlık, seviye, metin, SONRA ipucu) parçalarına böler; her parça bir sonraki eşit/üst başlığa kadar sürer."""
    lines = patch.split("\n")
    fenced = _code_fence_mask(lines)
    headings = [(index, len(match.group(1)), match.group(2).strip())
                for index, line in enumerate(lines) if not fenced[index] and (match := HEADING.match(line))]
    chunks, position = [], 0
    for index, level, title in headings:
        if index < position:
            continue
        end = next((other for other, other_level, _ in headings if other > index and other_level <= level), len(lines))
        hint = None
        if index > 0 and (match := AFTER_HINT.match(lines[index - 1])):
            hint = match.group(1)
        chunks.append((title, level, "\n".join(lines[index:end]).rstrip(), hint))
        position = end
    return chunks


def apply_patch(base: str, patch: str) -> tuple[str, int, int]:
    """Ajanın döndürdüğü değişen/yeni bölümleri mevcut belgeye uygular.

    Mevcut bir başlıkla eşleşen bölüm tamamen değiştirilir; eşleşmeyen yeni bölüm, '<!-- SONRA: başlık -->'
    ipucundaki bölümün ardına ya da belgenin sonuna eklenir. (belge, değiştirilen, eklenen) döndürür.
    """
    patch = strip_fences(patch)
    chunks = _patch_chunks(patch)
    if not chunks:
        return base, 0, 0
    # Ajan, belge başlığını (H1) koyup altına yalnızca değişen bölümleri yazdıysa H1 parçası belgenin
    # tamamını silmesin: mevcut alt bölümlerden biri bile yamada yoksa başlık satırı atılır ve alt
    # bölümler tek tek uygulanır. Hepsi varsa bu, belgenin tamamıdır ve olduğu gibi değiştirilir.
    if len(chunks) == 1 and chunks[0][1] == 1 and "\n" in chunks[0][2]:
        inner = _patch_chunks(chunks[0][2].split("\n", 1)[1])
        children = {_normalize(s.title) for s in split_sections(base) if s.kind == "heading" and s.level == 2}
        given = {_normalize(s.title) for s in split_sections(chunks[0][2]) if s.kind == "heading" and s.level == 2}
        if inner and children - given:
            chunks = inner
    replaced = added = 0
    for title, level, text, hint in chunks:
        headings = [section for section in split_sections(base) if section.kind == "heading"]
        lines = base.split("\n")
        match = (next((s for s in headings if _normalize(s.title) == _normalize(title) and s.level == level), None)
                 or next((s for s in headings if _normalize(s.title) == _normalize(title)), None))
        if match is not None:
            base = replace_section(base, match, text)
            replaced += 1
            continue
        anchor = next((s for s in headings if hint and _normalize(s.title) == _normalize(hint)), None)
        if anchor is not None:
            base = insert_after(base, anchor, text)
        else:
            base = "\n".join(lines).rstrip() + "\n\n" + text + "\n"
        added += 1
    return base, replaced, added
