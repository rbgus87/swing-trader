"""Manual parent ticker mapping for irregular preferred stock names.

Used as final fallback in guess_parent_ticker() when:
1. Name-based matching fails (e.g. abbreviated preferred names)
2. Ticker-based last-digit-zero rule fails

Add entries as discovered during collection.
"""

MANUAL_PARENT_MAP: dict[str, str] = {
    "008355": "008350",  # 남선알미우 → 남선알미늄
    "007815": "007810",  # 코리아써우 → 코리아써키트
}
