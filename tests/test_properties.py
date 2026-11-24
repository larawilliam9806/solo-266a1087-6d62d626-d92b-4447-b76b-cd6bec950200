"""Property tests: archived curve vs. the full-resolution curve.

These tests are deliberately independent of the implementation.  They check,
on random runs, that the stored polyline honours the tolerance at every
original sample and that the over-temperature set is identical to the original
polyline's -- verified symbolically by comparing maximal intervals, and also
numerically at many interior times.
"""

from __future__ import annotations

import random
from fractions import Fraction

import pytest
from fastapi.testclient import TestClient

from app.compression import above_limit_intervals
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    with TestClient(create_app(str(tmp_path / "archive.db"))) as test_client:
        yield test_client


def _value(points, t: Fraction):
    """Value of a polyline (list of (t, v)) at time t, or None outside."""
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t0 <= t <= t1:
            if t1 == t0:
                return Fraction(v0)
            return Fraction(v0) + Fraction((v1 - v0) * (t - t0), t1 - t0)
    return None


def _same_set(a, b) -> bool:
    return (
        len(a) == len(b)
        and all(
            x.start == y.start
            and x.end == y.end
            and x.start_closed == y.start_closed
            and x.end_closed == y.end_closed
            for x, y in zip(a, b)
        )
    )


def _sample_times(times, limit_intervals, count=400):
    lo, hi = Fraction(times[0]), Fraction(times[-1])
    step = (hi - lo) / (count + 1)
    samples = [lo + step * (k + 1) for k in range(count)]
    for iv in limit_intervals:
        for edge in (iv.start, iv.end):
            if lo <= edge <= hi:
                samples.extend([edge - Fraction(1, 10**6), edge, edge + Fraction(1, 10**6)])
    return samples


def test_random_runs_preserve_everything(client):
    rng = random.Random(31415)
    made = 0
    for case in range(120):
        n = rng.randint(2, 14)
        times = sorted(rng.sample(range(0, 120), n))
        centre = rng.randint(50, 150)
        values = [
            max(-40, min(300, centre + rng.randint(-60, 60))) for _ in range(n)
        ]
        limit = rng.randint(30, 170)
        tolerance = rng.choice([0, 0, 1, 2, 3, 5, 10])
        run_id = f"prop-{case}"
        body = {
            "run_id": run_id,
            "limit": limit,
            "tolerance": tolerance,
            "max_points": 10**6,
            "segments": [[[t, v] for t, v in zip(times, values)]],
        }
        response = client.post("/runs", json=body)
        assert response.status_code == 200, response.text
        made += 1
        record = client.get(f"/runs/{run_id}").json()
        kept = [(p["t"], p["v"]) for p in record["segments"][0]["points"]]
        assert kept[0] == (times[0], values[0])
        assert kept[-1] == (times[-1], values[-1])

        # tolerance at every original time
        for t, v in zip(times, values):
            assert abs(_value(kept, Fraction(t)) - v) <= tolerance

        # over-temperature set equality, symbolic
        original_intervals = above_limit_intervals(times, values, limit)
        archived_intervals = above_limit_intervals(
            [t for t, _ in kept], [v for _, v in kept], limit
        )
        assert _same_set(original_intervals, archived_intervals)

        # over-temperature set equality, numeric sweep at many interior times
        for t in _sample_times(times, original_intervals, 120):
            arch = _value(kept, t)
            if arch is None:
                continue
            orig = _value(list(zip(times, values)), t)
            assert (arch > limit) == (orig > limit), (run_id, t, arch, orig)

        # reported periods agree with the archived polyline
        reported = record["over_temperature_periods"]
        assert len(reported) == len(archived_intervals)
        for iv, item in zip(archived_intervals, reported):
            assert float(iv.start) == pytest.approx(item["start_ms"], abs=1e-9)
            assert float(iv.end) == pytest.approx(item["end_ms"], abs=1e-9)
            assert iv.start_closed == item["start_closed"]
            assert iv.end_closed == item["end_closed"]
    assert made == 120


def test_random_runs_all_have_at_most_source_points(client):
    rng = random.Random(27)
    for case in range(40):
        n = rng.randint(1, 12)
        times = sorted(rng.sample(range(0, 200), n))
        values = [rng.randint(0, 100) for _ in range(n)]
        body = {
            "run_id": f"size-{case}",
            "limit": 50,
            "tolerance": rng.randint(0, 10),
            "max_points": 10**6,
            "segments": [[[t, v] for t, v in zip(times, values)]],
        }
        payload = client.post("/runs", json=body).json()
        assert payload["point_count"] <= payload["source_point_count"]


def test_multiple_segments_are_archived_independently(client):
    body = {
        "run_id": "multi",
        "limit": 100,
        "tolerance": 0,
        "max_points": 1000,
        "segments": [
            [[0, 0], [10, 0], [20, 0]],
            [[100, 150], [110, 90], [120, 150]],
            [[200, 300], [210, 300]],
        ],
    }
    assert client.post("/runs", json=body).status_code == 200
    record = client.get("/runs/multi").json()
    counts = [seg["point_count"] for seg in record["segments"]]
    # flat, V-shaped, flat
    assert counts == [2, 3, 2]
    # over-limit periods stay within their own segments and never bridge gaps
    for period in record["over_temperature_periods"]:
        seg = record["segments"][period["segment_index"]]
        assert seg["start_ms"] <= period["start_ms"]
        assert period["end_ms"] <= seg["end_ms"]
    assert len(record["over_temperature_periods"]) == 3
