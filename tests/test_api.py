"""End-to-end tests against the FastAPI app with a temporary SQLite file."""

from __future__ import annotations

from fractions import Fraction

import pytest
from fastapi.testclient import TestClient

from app.compression import above_limit_intervals
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "archive.db"))
    with TestClient(app) as test_client:
        yield test_client


def _run_body(run_id="heat-1", *, max_points=100, limit=50, tolerance=5):
    return {
        "run_id": run_id,
        "limit": limit,
        "tolerance": tolerance,
        "max_points": max_points,
        "segments": [
            [[0, 0], [10, 20], [20, 40], [30, 60], [40, 80], [50, 100]],
            [[100, 100], [110, 80], [120, 60], [130, 40], [140, 20]],
        ],
    }


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_archive_and_read_back(client):
    response = client.post("/runs", json=_run_body())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_id"] == "heat-1"
    assert body["replaced"] is False
    assert body["point_count"] <= body["source_point_count"]
    assert body["source_point_count"] == 11

    # every archived segment keeps its original first and last sample
    source = _run_body()["segments"]
    for segment, archived in zip(source, body["segments"]):
        assert archived["points"][0] == {"t": segment[0][0], "v": segment[0][1]}
        assert archived["points"][-1] == {"t": segment[-1][0], "v": segment[-1][1]}

    fetched = client.get("/runs/heat-1")
    assert fetched.status_code == 200
    record = fetched.json()
    assert record["run_id"] == "heat-1"
    assert record["point_count"] == body["point_count"]
    assert len(record["segments"]) == 2
    # the archived polyline is above the limit exactly while the original is
    assert record["over_temperature_periods"]


def test_over_temperature_period_matches_full_resolution(client):
    # Ramp from 0 to 100 through limit 50 exactly at t=25.
    segments = [[[0, 0], [10, 25], [20, 45], [30, 70], [40, 90]]]
    body = {
        "run_id": "ramp",
        "limit": 50,
        "tolerance": 3,
        "max_points": 100,
        "segments": segments,
    }
    assert client.post("/runs", json=body).status_code == 200
    record = client.get("/runs/ramp").json()
    periods = record["over_temperature_periods"]
    assert len(periods) == 1
    # original crosses 50 between t=20 (45) and t=30 (70): 20 + 5/25*10 = 22
    assert periods[0]["start_ms"] == pytest.approx(22.0)
    assert periods[0]["end_ms"] == 40
    assert record["total_over_temperature_ms"] == pytest.approx(18.0)


def test_gap_between_segments_is_not_bridged(client):
    body = {
        "run_id": "gap",
        "limit": 50,
        "tolerance": 5,
        "max_points": 100,
        "segments": [
            [[0, 80], [10, 80]],
            [[500, 80], [510, 80]],
        ],
    }
    assert client.post("/runs", json=body).status_code == 200
    record = client.get("/runs/gap").json()
    assert len(record["segments"]) == 2
    # Both segments exceed the limit, yielding two disjoint periods, never one
    # spanning the missing interval.
    periods = record["over_temperature_periods"]
    assert len(periods) == 2
    assert periods[0]["end_ms"] == 10
    assert periods[1]["start_ms"] == 500


def test_missing_sample_breaks_flat_list_into_segments(client):
    body = {
        "run_id": "nulls",
        "limit": 50,
        "tolerance": 5,
        "max_points": 100,
        "segments": [[0, 80], [10, 80], None, [500, 80], [510, 80]],
    }
    assert client.post("/runs", json=body).status_code == 200
    record = client.get("/runs/nulls").json()
    assert len(record["segments"]) == 2


def test_point_limit_exceeded_keeps_old_archive(client):
    body = _run_body("keep-me", max_points=100)
    assert client.post("/runs", json=body).status_code == 200
    before = client.get("/runs/keep-me").json()

    # The peak must survive; a cap of 2 per segment cannot hold it.
    tight = _run_body("keep-me", max_points=2)
    response = client.post("/runs", json=tight)
    assert response.status_code == 200
    payload = response.json()
    assert payload["error"] == "point_limit_exceeded"
    assert payload["archived"] is False
    assert payload["required_points"] > 2
    assert isinstance(payload["required_points"], int)

    after = client.get("/runs/keep-me").json()
    assert after == before


def test_replacement_is_reported(client):
    assert client.post("/runs", json=_run_body()).status_code == 200
    second = client.post("/runs", json=_run_body())
    assert second.status_code == 200
    assert second.json()["replaced"] is True


def test_unknown_run_is_404(client):
    assert client.get("/runs/nope").status_code == 404


def test_list_runs(client):
    client.post("/runs", json=_run_body("a"))
    client.post("/runs", json=_run_body("b"))
    listing = client.get("/runs").json()["runs"]
    assert {item["run_id"] for item in listing} == {"a", "b"}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda body: body.pop("limit"),
        lambda body: body.pop("tolerance"),
        lambda body: body.pop("max_points"),
        lambda body: body.update(tolerance=-1),
        lambda body: body.update(segments=[]),
        lambda body: body.update(segments=[[[0, 1], [0, 2]]]),
        lambda body: body.update(segments=[[[0, 1.5], [10, 2]]]),
        lambda body: body.update(segments=[[[0, 1]]], max_points=-1),
    ],
)
def test_invalid_requests_are_422(client, mutation):
    body = _run_body()
    mutation(body)
    response = client.post("/runs", json=body)
    assert response.status_code == 422, response.text


def test_run_id_in_body_must_match_path(client):
    response = client.put("/runs/one", json=_run_body("two"))
    assert response.status_code == 422


def test_archive_by_path(client):
    response = client.put("/runs/path-id", json=_run_body(run_id=None))
    assert response.status_code == 200
    assert response.json()["run_id"] == "path-id"


def test_tolerance_is_enforced_at_original_times(client):
    # A noisy curve with small tolerance must keep enough samples to stay in band.
    values = [0, 10, 0, 10, 0, 10, 0, 10, 0, 10]
    times = list(range(0, 100, 10))
    body = {
        "run_id": "noisy",
        "limit": 1000,
        "tolerance": 2,
        "max_points": 100,
        "segments": [[[t, v] for t, v in zip(times, values)]],
    }
    assert client.post("/runs", json=body).status_code == 200
    record = client.get("/runs/noisy").json()
    points = record["segments"][0]["points"]
    kept = {p["t"]: p["v"] for p in points}
    assert kept[0] == 0 and kept[90] == 10

    def value_at(t: int) -> Fraction:
        archived_times = [p["t"] for p in points]
        for a, b in zip(points, points[1:]):
            if a["t"] <= t <= b["t"]:
                return Fraction(a["v"]) + Fraction(
                    (b["v"] - a["v"]) * (t - a["t"]), b["t"] - a["t"]
                )
        raise AssertionError

    for t, v in zip(times, values):
        assert abs(value_at(t) - v) <= 2


def test_over_limit_exactness_at_client_level(client):
    # Recompute above-limit periods from the archived polyline and full curve.
    segments = [[[0, 30], [10, 49], [20, 51], [30, 49], [40, 51], [50, 30]]]
    body = {
        "run_id": "wiggle",
        "limit": 50,
        "tolerance": 0,
        "max_points": 100,
        "segments": segments,
    }
    assert client.post("/runs", json=body).status_code == 200
    record = client.get("/runs/wiggle").json()
    archived = record["segments"][0]["points"]
    times = [p["t"] for p in archived]
    values = [p["v"] for p in archived]
    periods = above_limit_intervals(times, values, 50)
    reported = record["over_temperature_periods"]
    assert len(periods) == len(reported)
    for iv, item in zip(periods, reported):
        assert float(iv.start) == pytest.approx(item["start_ms"])
        assert float(iv.end) == pytest.approx(item["end_ms"])


# ---------------------------------------------------------------------------
# Tolerant input forms
# ---------------------------------------------------------------------------


def test_point_objects_and_aliased_keys(client):
    body = {
        "RunId": "aliases",
        "OverTemperatureLimit": 50,
        "TempTolerance": 0,
        "MaxPoints": 50,
        "Segments": [
            {
                "Points": [
                    {"TimeMs": 0, "Temperature": 10},
                    {"TimeMs": 10, "Temperature": 90},
                    {"TimeMs": 20, "Temperature": 10},
                ]
            }
        ],
    }
    response = client.post("/runs", json=body)
    assert response.status_code == 200, response.text
    # The peak is above the limit, so it must survive compression.
    assert response.json()["point_count"] == 3


def test_time_value_arrays(client):
    body = {
        "run_id": "arrays",
        "limit": 100,
        "tolerance": 0,
        "max_points": 50,
        "segments": [
            {"times": [0, 10, 20], "values": [0, 10, 20]},
            {"times": [100, 110], "values": [300, 300]},
        ],
    }
    assert client.post("/runs", json=body).status_code == 200
    record = client.get("/runs/arrays").json()
    # first segment is a straight line (2 points); second is flat (2 points)
    assert [seg["point_count"] for seg in record["segments"]] == [2, 2]


def test_parameters_can_be_nested(client):
    body = {
        "run_id": "nested",
        "parameters": {"limit": 50, "tolerance": 0, "max_points": 10},
        "points": [[0, 10], [10, 90], [20, 10]],
    }
    response = client.post("/runs", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["point_count"] == 3


def test_bare_list_body_with_query_parameters(client):
    response = client.post(
        "/runs?run_id=bare&limit=50&tolerance=0&max_points=10",
        json=[[0, 10], [10, 90], [20, 10]],
    )
    assert response.status_code == 200, response.text
    assert response.json()["run_id"] == "bare"


def test_put_with_run_id_in_path(client):
    response = client.put(
        "/runs/path-run",
        json={
            "limit": 50,
            "tolerance": 0,
            "max_points": 10,
            "segments": [[[0, 10], [10, 90]]],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["run_id"] == "path-run"


def test_integral_floats_are_accepted(client):
    body = {
        "run_id": "floats",
        "limit": 217.0,
        "tolerance": 2.0,
        "max_points": 10,
        "segments": [[[0.0, 25.0], [10.0, 30.0]]],
    }
    assert client.post("/runs", json=body).status_code == 200


def test_non_integral_sample_time_is_rejected(client):
    body = {
        "run_id": "bad",
        "limit": 50,
        "tolerance": 0,
        "max_points": 10,
        "segments": [[[0, 10], [10.5, 90]]],
    }
    assert client.post("/runs", json=body).status_code == 422


def test_string_numbers_are_accepted(client):
    body = {
        "run_id": "strings",
        "limit": "50",
        "tolerance": "2",
        "max_points": "10",
        "segments": [[["0", "10"], ["10", "90"]]],
    }
    assert client.post("/runs", json=body).status_code == 200


def test_duplicate_run_ids_are_independent(client):
    for run_id in ("a", "b"):
        body = _run_body(run_id)
        assert client.post("/runs", json=body).status_code == 200
    assert client.get("/runs/a").json()["run_id"] == "a"
    assert client.get("/runs/b").json()["run_id"] == "b"


def test_cap_exceeded_does_not_create_new_run(client):
    body = {
        "run_id": "fresh",
        "limit": 50,
        "tolerance": 0,
        "max_points": 1,
        "segments": [[[0, 10], [10, 90]]],
    }
    response = client.post("/runs", json=body)
    assert response.status_code == 200
    assert response.json()["archived"] is False
    assert response.json()["required_points"] == 2
    assert client.get("/runs/fresh").status_code == 404


def test_cap_allows_exact_minimum(client):
    body = {
        "run_id": "exact",
        "limit": 50,
        "tolerance": 0,
        "max_points": 3,
        "segments": [[[0, 10], [10, 90], [20, 10]]],
    }
    response = client.post("/runs", json=body)
    assert response.status_code == 200
    assert response.json()["point_count"] == 3


def test_shipped_example_file(client):
    import json
    from pathlib import Path

    sample = Path(__file__).resolve().parent.parent / "examples" / "sample_run.json"
    body = json.loads(sample.read_text(encoding="utf-8"))
    response = client.post("/runs", json=body)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source_point_count"] == 75
    assert payload["point_count"] == 10
    assert [seg["point_count"] for seg in payload["segments"]] == [8, 2]
    periods = payload["over_temperature_periods"]
    assert len(periods) == 1
    assert periods[0]["segment_index"] == 0


def test_archive_survives_app_restart(tmp_path):
    db_path = str(tmp_path / "persist.db")
    body = {
        "run_id": "durable",
        "limit": 50,
        "tolerance": 0,
        "max_points": 10,
        "segments": [[[0, 10], [10, 90], [20, 10]]],
    }
    first = TestClient(create_app(db_path))
    assert first.post("/runs", json=body).status_code == 200
    before = first.get("/runs/durable").json()

    # A fresh app instance on the same SQLite file must see the archive.
    second = TestClient(create_app(db_path))
    after = second.get("/runs/durable").json()
    for key in ("run_id", "segments", "point_count", "limit", "tolerance"):
        assert after[key] == before[key]
    assert after["over_temperature_periods"] == before["over_temperature_periods"]


def test_replacing_preserves_created_at(client):
    body = _run_body("ts")
    first = client.post("/runs", json=body).json()["archive"]
    second = client.post("/runs", json=body).json()["archive"]
    assert second["created_at"] == first["created_at"]
    assert second["updated_at"] >= first["updated_at"]


def test_concurrent_archives(tmp_path):
    import concurrent.futures

    db_path = str(tmp_path / "concurrent.db")
    app = create_app(db_path)

    def store(i: int) -> int:
        with TestClient(app) as worker:
            body = {
                "run_id": f"c-{i}",
                "limit": 50,
                "tolerance": 0,
                "max_points": 10,
                "segments": [[[0, 10], [10, 90], [20, 10]]],
            }
            return worker.post("/runs", json=body).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(store, range(24)))
    assert codes == [200] * 24

    with TestClient(app) as reader:
        listing = reader.get("/runs").json()["runs"]
    assert len(listing) == 24
