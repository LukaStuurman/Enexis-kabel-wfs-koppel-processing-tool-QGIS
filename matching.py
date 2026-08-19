# -*- coding: utf-8 -*-
"""Pure-Python helpers for Enexis cable matching."""

from __future__ import annotations

import math
import re
from typing import List, Sequence, Tuple

_PREFIX_RE = re.compile(r"^\s*Kabelgroup\s*:\s*", re.IGNORECASE)


def normalize_label(value) -> str:
    """Return the exact cable subgroup key after removing ``Kabelgroup:``.

    Only leading/trailing whitespace and the single known WFS prefix are
    normalized. Case and all other characters are kept unchanged so the
    subgroup comparison remains exact.
    """
    if value is None:
        return ""
    text = str(value).strip()
    text = _PREFIX_RE.sub("", text, count=1)
    return text.strip()


def parse_decimal(value) -> float:
    """Parse Dutch or dot-decimal numeric text to float."""
    if value is None:
        raise ValueError("lege waarde")

    text = str(value).strip().replace("\u00a0", "").replace(" ", "")
    if not text:
        raise ValueError("lege waarde")

    if "," in text and "." in text:
        # The right-most separator is treated as decimal separator.
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")

    number = float(text)
    if not math.isfinite(number):
        raise ValueError("waarde is niet eindig")
    return number


def _sort_items(items: Sequence[Tuple[int, float]]) -> List[Tuple[int, float]]:
    return sorted(items, key=lambda item: (item[1], item[0]))


def _match_smaller_to_larger(
    smaller: Sequence[Tuple[int, float]],
    larger: Sequence[Tuple[int, float]],
) -> List[Tuple[int, int]]:
    """Match every item in ``smaller`` to a unique item in ``larger``.

    In one dimension, an optimal absolute-distance assignment never needs
    crossing pairs after both sides are sorted by length. Equal-sized groups
    therefore reduce to a simple sorted zip. Unequal groups use dynamic
    programming to select the optimal ordered subset from the larger side.

    The DP keeps only two cost rows in memory. Backtracking choices use
    bytearrays instead of Python bool/object matrices, greatly reducing memory
    use for large duplicate cable groups.
    """
    a = _sort_items(smaller)
    b = _sort_items(larger)
    n, m = len(a), len(b)

    if n == 0:
        return []
    if n > m:
        raise ValueError("smaller mag niet groter zijn dan larger")

    # Fast path: when both sides contain the same number of items, sorted
    # position-to-position matching is globally optimal for absolute distance.
    # This changes the common case from O(n^2) DP to O(n log n) sorting.
    if n == m:
        return [(a[i][0], b[i][0]) for i in range(n)]

    # Fast path for a single item: just take the nearest candidate. Tie-break
    # by length and original index for deterministic output.
    if n == 1:
        left_idx, left_length = a[0]
        right_idx, _ = min(
            b,
            key=lambda item: (
                abs(left_length - item[1]),
                item[1],
                item[0],
            ),
        )
        return [(left_idx, right_idx)]

    inf = float("inf")
    previous = [0.0] * (m + 1)

    # One byte per DP decision instead of a Python bool object plus a complete
    # float matrix. CPU remains O(n*m), but memory drops to roughly n*m bytes
    # plus two float rows.
    take = [bytearray(m + 1) for _ in range(n + 1)]

    for i in range(1, n + 1):
        current = [inf] * (m + 1)
        left_length = a[i - 1][1]

        # At least i larger-side candidates are needed to match i items.
        for j in range(i, m + 1):
            skip_cost = current[j - 1]
            match_cost = previous[j - 1] + abs(left_length - b[j - 1][1])

            # Prefer the earlier candidate on an exact tie for stable output.
            if match_cost < skip_cost or math.isinf(skip_cost):
                current[j] = match_cost
                take[i][j] = 1
            else:
                current[j] = skip_cost

        previous = current

    pairs: List[Tuple[int, int]] = []
    i, j = n, m
    while i > 0 and j > 0:
        if take[i][j]:
            pairs.append((a[i - 1][0], b[j - 1][0]))
            i -= 1
            j -= 1
        else:
            j -= 1

    if i != 0:
        raise RuntimeError("interne fout bij één-op-één matching")

    pairs.reverse()
    return pairs


def optimal_one_to_one(
    left: Sequence[Tuple[int, float]],
    right: Sequence[Tuple[int, float]],
) -> List[Tuple[int, int]]:
    """Return globally optimal one-to-one pairs by absolute length difference.

    Items are ``(original_index, length)``. Every item on the smaller side is
    matched exactly once; every item on the larger side is used at most once.
    """
    if not left or not right:
        return []

    if len(left) <= len(right):
        return _match_smaller_to_larger(left, right)

    inverted = _match_smaller_to_larger(right, left)
    return [(left_idx, right_idx) for right_idx, left_idx in inverted]
