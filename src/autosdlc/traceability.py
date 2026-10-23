"""Gereksinim kimliklerini test dosyalarına bağlayan izlenebilirlik matrisi."""

from __future__ import annotations

import re


# FR-01, NFR-2 ve test adlarında kullanılan test_fr01_ / test_FR_01 biçimlerini tanır.
REQUIREMENT_ID = re.compile(r"(?<![A-Za-z])(N?FR)[-_]?(\d{1,3})(?!\d)", re.IGNORECASE)


def requirement_ids(text: str) -> list[str]:
    """Metindeki FR/NFR kimliklerini ilk görülme sırasıyla, FR-01 biçimine getirerek döndürür."""
    seen: dict[str, None] = {}
    for kind, number in REQUIREMENT_ID.findall(text):
        seen.setdefault(f"{kind.upper()}-{int(number):02d}", None)
    return list(seen)


def build_matrix(requirements: str, test_files: dict[str, str]) -> tuple[str, dict[str, float]]:
    """Markdown matris ve FR/NFR kapsama oranlarını döndürür."""
    ids = requirement_ids(requirements)
    referenced = {name: set(requirement_ids(content)) for name, content in test_files.items()}
    covered = {req: [name for name, refs in referenced.items() if req in refs] for req in ids}
    coverage = {}
    for kind in ("FR", "NFR"):
        group = [req for req in ids if req.startswith(kind + "-")]
        coverage[kind] = sum(bool(covered[req]) for req in group) / len(group) if group else 0.0
    rows = "\n".join(
        f"| {req} | {', '.join(f'`{name}`' for name in covered[req]) or '—'} | {'Kapsanıyor' if covered[req] else 'Test yok'} |"
        for req in ids
    )
    missing = [req for req in ids if req.startswith("FR-") and not covered[req]]
    content = (
        "# İzlenebilirlik matrisi\n\n"
        "Gereksinim kimlikleri `requirements.md`, test bağlantıları test dosyalarındaki "
        "kimlik referanslarından otomatik çıkarılmıştır.\n\n"
        f"- İşlevsel gereksinim (FR) kapsamı: **%{coverage['FR'] * 100:.0f}**\n"
        f"- İşlevsel olmayan gereksinim (NFR) kapsamı: **%{coverage['NFR'] * 100:.0f}**\n"
        f"- Testi olmayan FR: {', '.join(missing) or 'yok'}\n\n"
        "| Gereksinim | Test dosyaları | Durum |\n| --- | --- | --- |\n"
        f"{rows}\n"
    )
    return content, coverage
