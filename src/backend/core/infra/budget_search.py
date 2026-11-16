"""Find the largest size that still fits a budget, without shrinking one step at a time."""
from __future__ import annotations

from typing import Callable


def largest_fitting(low: int, high: int, fits: Callable[[int], bool]) -> int:
    """Largest ``n`` in ``[low, high]`` with ``fits(n)``, or ``low`` when none does.

    ``fits`` must be monotone: once a size fits, every smaller one does too.
    Checks ``high`` first, so input already within budget costs one call.
    """
    if fits(high):
        return high
    while low < high:
        mid = (low + high + 1) // 2
        if fits(mid):
            low = mid
        else:
            high = mid - 1
    return low
