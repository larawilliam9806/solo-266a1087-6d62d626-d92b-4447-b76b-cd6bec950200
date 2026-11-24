"""Minimal-point compression of piecewise-linear temperature curves.

A furnace run arrives as one or more *segments* of measured samples.  Missing
measurements break the curve: segments are never joined, and every segment
keeps its own first and last sample.

For one segment with samples ``(t_0, v_0) ... (t_{n-1}, v_{n-1})`` (strictly
increasing integer times, integer temperatures) the archive keeps a subset of
indices containing ``0`` and ``n-1``.  The archived curve is the
piecewise-linear interpolation through that subset.  Two conditions hold.

``tolerance``
    At every *original* sample time the archived curve differs from the
    measured value by at most ``tolerance``.  Comparisons use exact rational
    arithmetic, so rounding can never slip a sample outside the band.

``over-temperature set``
    ``{t : archived(t) > limit}`` equals ``{t : original(t) > limit}``, where
    ``original`` joins every measured sample point by point.

The task is to archive as few points as possible.  Modelling sample indices as
vertices and admissible chords as edges, a minimum-cardinality archive is a
shortest path from ``0`` to ``n-1``.  Edges have unit weight, so breadth-first
search yields the exact minimum.

A chord ``i -> j`` is admissible when:

* it stays inside the tolerance band at all interior original sample times;
* it reproduces the original above-limit set on its own span.

The tolerance test is incremental: for fixed ``i`` the set of admissible chord
slopes is one interval that only tightens as ``j`` grows, so it costs O(1) per
chord and can stop early when the interval becomes empty.  The above-limit
test is O(1) per chord (one binary search in the worst case) against the
precomputed list of maximal above-limit intervals.  Chords partition the
segment's time axis, and archived values equal measured values at kept
samples, so per-chord equality of above-limit sets is necessary and sufficient
for global equality.

Cost is O(n^2) worst case and O(n) memory per segment; smooth / flat curves
are found in far fewer chord checks because BFS stops at the first target.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from fractions import Fraction
from typing import Sequence

__all__ = [
    "Interval",
    "above_limit_intervals",
    "compress_segment",
    "fraction_to_json_number",
    "to_fraction",
]


@dataclass(frozen=True)
class Interval:
    """A maximal time interval on which the curve is strictly above a limit.

    ``start_closed`` / ``end_closed`` describe membership of the interval's
    own endpoints.  Interior endpoints are always open, because a strict
    inequality cannot include the crossing point.
    """

    start: Fraction
    start_closed: bool
    end: Fraction
    end_closed: bool

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("interval end precedes start")
        if self.end == self.start and not (self.start_closed and self.end_closed):
            raise ValueError("degenerate interval must be closed on both ends")


def to_fraction(value: int | float | str | Fraction) -> Fraction:
    """Convert a JSON-facing number to an exact rational.

    Floats are interpreted through their shortest decimal representation, so
    ``0.1`` means one tenth rather than the nearest binary float.
    """
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("number must be finite")
        return Fraction(str(value))
    if isinstance(value, str):
        return Fraction(value)
    raise ValueError(f"cannot convert {type(value).__name__} to a number")


def _crossing(t0: int, v0: int, t1: int, v1: int, limit: Fraction) -> Fraction:
    """Time at which the directed edge (t0,v0)-(t1,v1) equals ``limit``."""
    return Fraction(t0) + Fraction(limit - v0) * (t1 - t0) / (v1 - v0)


def above_limit_intervals(
    times: Sequence[int],
    values: Sequence[int],
    limit: int | float | str | Fraction,
) -> list[Interval]:
    """Maximal intervals of time where the polyline is strictly above limit.

    ``times`` must be strictly increasing.  A lone sample has no temporal
    extent and therefore contributes no interval.
    """
    if len(times) != len(values):
        raise ValueError("times and values must have the same length")
    lim = to_fraction(limit)
    if len(times) <= 1:
        return []

    pieces: list[Interval] = []
    for k in range(len(times) - 1):
        t0, t1 = times[k], times[k + 1]
        v0, v1 = values[k], values[k + 1]
        if v0 > lim and v1 > lim:
            piece = Interval(Fraction(t0), True, Fraction(t1), True)
        elif v0 > lim:  # v1 <= limit
            piece = Interval(Fraction(t0), True, _crossing(t0, v0, t1, v1, lim), False)
        elif v1 > lim:  # v0 <= limit
            piece = Interval(_crossing(t0, v0, t1, v1, lim), False, Fraction(t1), True)
        else:
            continue

        prev = pieces[-1] if pieces else None
        if (
            prev is not None
            and prev.end == piece.start
            and prev.end_closed
            and piece.start_closed
        ):
            pieces[-1] = Interval(
                prev.start, prev.start_closed, piece.end, piece.end_closed
            )
        else:
            pieces.append(piece)
    return pieces


def fraction_to_json_number(value: Fraction) -> int | float:
    """Return an ``int`` for integral values, otherwise a ``float``."""
    if value.denominator == 1:
        return value.numerator
    return float(value)


def _contains(interval: Interval, t: Fraction) -> bool:
    if t < interval.start or t > interval.end:
        return False
    if t == interval.start and not interval.start_closed:
        return False
    if t == interval.end and not interval.end_closed:
        return False
    return True


def _rat_max(a: tuple[int, int] | None, b: tuple[int, int]) -> tuple[int, int]:
    if a is None or b[0] * a[1] > a[0] * b[1]:
        return b
    return a


def _rat_min(a: tuple[int, int] | None, b: tuple[int, int]) -> tuple[int, int]:
    if a is None or b[0] * a[1] < a[0] * b[1]:
        return b
    return a


def _rat_gt(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] * b[1] > b[0] * a[1]


def compress_segment(
    times: Sequence[int],
    values: Sequence[int],
    limit: int | float | str | Fraction,
    tolerance: int | float | str | Fraction,
) -> list[int]:
    """Return the index sequence of a minimum-size valid archived subset.

    The result is strictly increasing and contains ``0`` and
    ``len(times) - 1``.  ``tolerance`` must be non-negative.
    """
    tol = to_fraction(tolerance)
    if tol < 0:
        raise ValueError("tolerance must be non-negative")
    lim = to_fraction(limit)
    if len(times) != len(values):
        raise ValueError("times and values must have the same length")
    n = len(times)
    if n == 0:
        raise ValueError("a segment needs at least one sample")
    for a, b in zip(times, times[1:]):
        if b <= a:
            raise ValueError("sample times must strictly increase within a segment")
    if n == 1:
        return [0]
    if n == 2:
        return [0, 1]

    intervals = above_limit_intervals(times, values, lim)
    interval_starts = [iv.start for iv in intervals]

    # Which maximal above-limit interval owns each above-limit sample.
    vertex_interval = [-1] * n
    p = 0
    for k in range(n):
        if values[k] > lim:
            t = Fraction(times[k])
            while p < len(intervals) and not _contains(intervals[p], t):
                p += 1
            if p >= len(intervals):  # pragma: no cover - defensive
                raise AssertionError("sample above limit has no interval")
            vertex_interval[k] = p

    tf = [Fraction(t) for t in times]

    def above_ok(i: int, j: int) -> bool:
        """Does chord i->j reproduce the original above-limit set on [ti,tj]?"""
        vi, vj = values[i], values[j]

        if vi > lim and vj > lim:
            # Chord above everywhere; original must never dip below.
            return vertex_interval[i] == vertex_interval[j]

        if vi > lim:  # vj <= limit
            # Chord above on [ti, tau), at/below after.
            tau = _crossing(times[i], vi, times[j], vj, lim)
            pi = vertex_interval[i]
            iv = intervals[pi]
            if iv.end != tau or iv.end_closed:
                return False
            if pi + 1 < len(intervals) and interval_starts[pi + 1] < tf[j]:
                return False
            return True

        if vj > lim:  # vi <= limit
            # Chord below before tau, above on (tau, tj].
            tau = _crossing(times[i], vi, times[j], vj, lim)
            pj = vertex_interval[j]
            iv = intervals[pj]
            if iv.start != tau or iv.start_closed:
                return False
            if pj > 0 and intervals[pj - 1].end > tf[i]:
                return False
            return True

        # Both ends at or below the limit: the chord is nowhere above it, so
        # no original above-limit interval may start inside [ti, tj).
        idx = bisect_left(interval_starts, tf[i])
        return not (idx < len(intervals) and interval_starts[idx] < tf[j])

    # Tolerance as an exact rational p/q keeps every bound integral.
    p_num = tol.numerator
    p_den = tol.denominator

    parent = [-1] * n
    dist = [-1] * n
    dist[0] = 0
    frontier = [0]
    level = 0

    while frontier and dist[n - 1] < 0:
        next_frontier: list[int] = []
        for i in frontier:
            ti, vi = times[i], values[i]
            lo: tuple[int, int] | None = None
            hi: tuple[int, int] | None = None
            for j in range(i + 1, n):
                if j > i + 1:
                    k = j - 1
                    dxq = (times[k] - ti) * p_den
                    lo = _rat_max(lo, ((values[k] - vi) * p_den - p_num, dxq))
                    hi = _rat_min(hi, ((values[k] - vi) * p_den + p_num, dxq))
                    if lo is not None and hi is not None and _rat_gt(lo, hi):
                        # The slope interval only shrinks from here on.
                        break

                dy = values[j] - vi
                dxj = times[j] - ti
                if lo is not None and hi is not None:
                    if not (
                        lo[0] * dxj <= dy * lo[1] and dy * hi[1] <= hi[0] * dxj
                    ):
                        continue

                if not above_ok(i, j):
                    continue

                if dist[j] < 0:
                    dist[j] = level + 1
                    parent[j] = i
                    if j == n - 1:
                        break
                    next_frontier.append(j)
            if dist[n - 1] >= 0:
                break
        frontier = next_frontier
        level += 1

    if dist[n - 1] < 0:  # pragma: no cover - adjacent chords are always valid
        raise AssertionError("no valid compression path")

    out: list[int] = []
    k = n - 1
    while k != -1:
        out.append(k)
        k = parent[k]
    out.reverse()
    return out
