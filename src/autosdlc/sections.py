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
