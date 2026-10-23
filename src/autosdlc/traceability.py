"""Traceability matrix linking requirement ids to the test files."""

from __future__ import annotations

import re

from .i18n import t


# Recognises FR-01, NFR-2 and the test_fr01_ / test_FR_01 forms used in test names.
REQUIREMENT_ID = re.compile(r"(?<![A-Za-z])(N?FR)[-_]?(\d{1,3})(?!\d)", re.IGNORECASE)


def requirement_ids(text: str) -> list[str]:
    """Returns the FR/NFR ids in the text in order of first appearance, normalized to the FR-01 form."""
    seen: dict[str, None] = {}
    for kind, number in REQUIREMENT_ID.findall(text):
        seen.setdefault(f"{kind.upper()}-{int(number):02d}", None)
    return list(seen)


def build_matrix(requirements: str, test_files: dict[str, str], language: str | None = None) -> tuple[str, dict[str, float]]:
    """Returns the Markdown matrix (in the document language) and the FR/NFR coverage ratios."""
    ids = requirement_ids(requirements)
    referenced = {name: set(requirement_ids(content)) for name, content in test_files.items()}
    covered = {req: [name for name, refs in referenced.items() if req in refs] for req in ids}
    coverage = {}
    for kind in ("FR", "NFR"):
        group = [req for req in ids if req.startswith(kind + "-")]
        coverage[kind] = sum(bool(covered[req]) for req in group) / len(group) if group else 0.0
    rows = "\n".join(
        f"| {req} | {', '.join(f'`{name}`' for name in covered[req]) or '—'} | {t('Covered', language=language) if covered[req] else t('No test', language=language)} |"
        for req in ids
    )
    missing = [req for req in ids if req.startswith("FR-") and not covered[req]]
    content = (
        f"# {t('Traceability matrix', language=language)}\n\n"
        + t("Requirement ids come from `requirements.md`; the test links are extracted automatically from the "
            "id references in the test files.", language=language) + "\n\n"
        + f"- {t('Functional requirement (FR) coverage', language=language)}: **{coverage['FR'] * 100:.0f}%**\n"
        + f"- {t('Non-functional requirement (NFR) coverage', language=language)}: **{coverage['NFR'] * 100:.0f}%**\n"
        + f"- {t('FRs without a test', language=language)}: {', '.join(missing) or t('none', language=language)}\n\n"
        + f"| {t('Requirement', language=language)} | {t('Test files', language=language)} | {t('Status', language=language)} |\n"
        + "| --- | --- | --- |\n"
        + f"{rows}\n"
    )
    return content, coverage
