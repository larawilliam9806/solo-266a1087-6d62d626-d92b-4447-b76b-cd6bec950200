"""Tests for the compression core, including a brute-force minimality check."""

from __future__ import annotations

import itertools
import random
from fractions import Fraction

import pytest

from app.compression import (
    Interval,
    above_limit_intervals,
    compress_segment,
    fraction_to_json_number,
    to_fraction,
)


def _archived_value(times: list[int], values: list[int], keep: list[int], t: int) -> Fraction:
    """Exact value of the archived polyline at sample time ``t``."""
    for a, b in zip(keep, keep[1:]):
        if times[a] <= t <= times[b]:
            if times[b] == times[a]:
                return Fraction(values[a])
            return Fraction(values[a]) + Fraction(
                (values[b] - values[a]) * (t - times[a]), times[b] - times[a]
            )
    raise AssertionError(f"t={t} not covered by keep={keep}")


def _intervals_equal(a: list[Interval], b: list[Interval]) -> bool:
    if len(a) != len(b):
        return False
    return all(
        x.start == y.start
        and x.end == y.end
        and x.start_closed == y.start_closed
        and x.end_closed == y.end_closed
        for x, y in zip(a, b)
    )


def _valid(times: list[int], values: list[int], keep: list[int], limit: int, tol: int) -> bool:
    for k, t in enumerate(times):
        if abs(_archived_value(times, values, keep, t) - values[k]) > tol:
            return False
    original = above_limit_intervals(times, values, limit)
    archived = above_limit_intervals(
        [times[i] for i in keep], [values[i] for i in keep], limit
    )
    return _intervals_equal(original, archived)


def brute_force_min(
    times: list[int], values: list[int], limit: int, tolerance: int
) -> int:
    n = len(times)
    if n <= 2:
        return n
    best = n
    interior = list(range(1, n - 1))
    for mask in range(1 << len(interior)):
        keep = [0] + [interior[i] for i in range(len(interior)) if mask >> i & 1] + [n - 1]
        if len(keep) >= best:
            continue
        if _valid(times, values, keep, limit, tolerance):
            best = len(keep)
    return best


def test_above_limit_intervals_basic():
    # rises through 50 at t=50, falls through 50 at t=150
    times = [0, 100, 200]
    values = [0, 100, 0]
    (iv,) = above_limit_intervals(times, values, 50)
    assert iv.start == 50 and not iv.start_closed
    assert iv.end == 150 and not iv.end_closed

    # above from the first sample, below after t=150
    (iv,) = above_limit_intervals([0, 100, 200], [60, 100, 0], 50)
    assert iv.start == 0 and iv.start_closed
    assert iv.end == 150 and not iv.end_closed


def test_above_limit_interval_closed_touch_points():
    # a plateau exactly on the limit is not above it
    assert above_limit_intervals([0, 100], [50, 50], 50) == []
    # touching the limit at an interior sample does not glue intervals whose
    # ends are open there
    intervals = above_limit_intervals([0, 100, 200], [60, 50, 60], 50)
    assert len(intervals) == 2
    assert intervals[0].end == 100 and not intervals[0].end_closed
    assert intervals[1].start == 100 and not intervals[1].start_closed
    # nothing above
    assert above_limit_intervals([0, 100], [0, 10], 50) == []


def test_compress_keeps_endpoints():
    keep = compress_segment([0, 100, 200], [10, 10, 10], 50, 0)
    assert keep[0] == 0 and keep[-1] == 2


def test_compress_flat_curve_uses_two_points():
    times = list(range(0, 1000, 10))
    values = [42] * len(times)
    keep = compress_segment(times, values, 100, 0)
    assert keep == [0, len(times) - 1]


def test_compress_linear_curve_uses_two_points():
    times = list(range(0, 1000, 7))
    values = [3 * t for t in times]
    keep = compress_segment(times, values, 10**9, 0)
    assert keep == [0, len(times) - 1]


def test_compress_respects_tolerance_band():
    times = [0, 10, 20, 30, 40]
    values = [0, 10, 0, 10, 0]
    keep = compress_segment(times, values, 1000, 5)
    assert _valid(times, values, keep, 1000, 5)


def test_over_limit_chord_cannot_flatten_peak():
    # Archiving [0, ... , 4] directly would draw a flat chord under the limit,
    # erasing the above-limit period, so the peak sample must survive.
    times = [0, 10, 20]
    values = [0, 100, 0]
    keep = compress_segment(times, values, 50, 1000)
    assert keep == [0, 1, 2]


def test_minimality_against_brute_force_random():
    rng = random.Random(20261123)
    for trial in range(400):
        n = rng.randint(2, 9)
        times = sorted(rng.sample(range(0, 60), n))
        values = [rng.randint(-20, 120) for _ in range(n)]
        limit = rng.randint(0, 100)
        tolerance = rng.randint(0, 40)
        keep = compress_segment(times, values, limit, tolerance)
        assert keep[0] == 0 and keep[-1] == n - 1
        assert keep == sorted(set(keep)), keep
        assert _valid(times, values, keep, limit, tolerance), (
            trial,
            times,
            values,
            limit,
            tolerance,
            keep,
        )
        expected = brute_force_min(times, values, limit, tolerance)
        assert len(keep) == expected, (
            trial,
            times,
            values,
            limit,
            tolerance,
            keep,
            expected,
        )


def test_minimality_with_non_integral_tolerance_and_limit():
    rng = random.Random(7)
    for trial in range(60):
        n = rng.randint(2, 8)
        times = sorted(rng.sample(range(0, 40), n))
        values = [rng.randint(-10, 60) for _ in range(n)]
        limit = Fraction(rng.randint(0, 50), 2)
        tolerance = Fraction(rng.randint(0, 20), 4)
        keep = compress_segment(times, values, limit, tolerance)
        for k, t in enumerate(times):
            if abs(_archived_value(times, values, keep, t) - values[k]) > tolerance:
                pytest.fail(f"tolerance violated: {trial} {times} {values} {keep}")
        original = above_limit_intervals(times, values, limit)
        archived = above_limit_intervals(
            [times[i] for i in keep], [values[i] for i in keep], limit
        )
        assert _intervals_equal(original, archived)


def test_fraction_json_number():
    assert fraction_to_json_number(Fraction(3)) == 3
    assert isinstance(fraction_to_json_number(Fraction(3)), int)
    assert fraction_to_json_number(Fraction(1, 2)) == 0.5
    assert to_fraction(0.1) == Fraction(1, 10)


@pytest.mark.parametrize(
    "times,values,tol,limit",
    [
        ([0, 10, 20, 30], [0, 100, 0, 100], 0, 50),
        ([0, 10, 20, 30, 40], [0, 50, 0, 50, 0], 1, 25),
    ],
)
def test_edge_cases_are_valid(times, values, tol, limit):
    keep = compress_segment(times, values, limit, tol)
    assert _valid(times, values, keep, limit, tol)
