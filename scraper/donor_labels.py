"""Conservative display-label normalization for donor aggregations.

This does not resolve identities: punctuation, word order, and different words
remain distinct. Only whitespace and capitalization variants share a key.
"""

from __future__ import annotations

from collections.abc import Iterable


MISC_CASH_LABEL = "Miscellaneous Cash Contributions $100 and under"


def normalize_donor_label(value: object) -> str:
    """Trim/collapse whitespace and give the pooled cash category one label."""
    if not isinstance(value, str):
        return ""
    label = " ".join(value.split())
    return MISC_CASH_LABEL if label.lower() == MISC_CASH_LABEL.lower() else label


def donor_label_key(value: object) -> str:
    """Whitespace- and case-insensitive key, matching SQL's lower() rule."""
    return normalize_donor_label(value).lower()


def build_donor_label_map(labels: Iterable[object]) -> dict[str, str]:
    """Choose a stable, readable spelling for every normalized label key.

    Prefer an existing mixed-case spelling, then break ties lexicographically
    so transaction order and the reporting period cannot choose the spelling.
    Preserve acronyms and punctuation rather than applying title case.
    """
    display: dict[str, str] = {}
    for value in labels:
        label = normalize_donor_label(value)
        key = donor_label_key(label)
        rank = (label.isupper() or label.islower(), label)
        prior = display.get(key)
        if prior is None or rank < (
            prior.isupper() or prior.islower(), prior
        ):
            display[key] = label
    return display
