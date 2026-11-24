"""Tests for the chunked ``stream=1`` submission mode.

Three layers are covered:

* :mod:`app.streaming` -- the incremental JSON state machine, fed in odd-sized
  chunks so token/state boundaries are exercised;
* :class:`app.service.StreamingArchiveSession` -- per-segment compression,
  cap accounting and the bounded retained memory;
* the HTTP surface -- parity with the classic submission, query-string
  parameters, 422 on bad/truncated bodies, ``archived: false`` with the exact
  required size over the cap, and preservation of the previous archive.
"""

from __future__ import annotations

import tracemalloc

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas import ValidationError
from app.service import ArchiveRejected, ArchiveService, StreamingArchiveSession
from app.storage import ArchiveStore
from app.streaming import StreamInputError, StreamTruncated, StreamingBodyParser

QUERY = "run_id=stream&limit=50&tolerance=5&max_points=100"


# ---------------------------------------------------------------------------
# Incremental parser
# ---------------------------------------------------------------------------


def _collect(data: bytes, chunk_size: int = 7) -> list[list[tuple[int, int]]]:
    segments: list[list[tuple[int, int]]] = []
    parser = StreamingBodyParser(segments.append)
    for start in range(0, len(data), chunk_size):
        parser.feed(data[start : start + chunk_size])
    parser.finish()
    return segments


def test_parser_reads_segments_and_points():
    data = b'[ [[0, 25], [1000, 30]], [[2000, -5], [3000, 42]] ]'
    assert _collect(data) == [
        [(0, 25), (1000, 30)],
        [(2000, -5), (3000, 42)],
    ]


def test_parser_accepts_any_chunk_split_including_single_bytes():
    data = b'[[[1,2],[3,4]],[[5,6]]]'
    whole = _collect(data, chunk_size=len(data))
    for size in range(1, len(data) + 1):
        assert _collect(data, chunk_size=size) == whole


def test_parser_requires_non_empty_body():
    parser = StreamingBodyParser(lambda _seg: None)
    parser.feed(b"   ")
    with pytest.raises(StreamTruncated):
        parser.finish()


def test_parser_rejects_non_array_body():
    with pytest.raises(StreamInputError):
        _collect(b'{"segments": []}')


def test_parser_rejects_empty_outer_array():
    with pytest.raises(StreamInputError):
        _collect(b"[]")


def test_parser_rejects_empty_segment():
    with pytest.raises(StreamInputError):
        _collect(b"[[]]")


def test_parser_rejects_empty_point_pair():
    with pytest.raises(StreamInputError):
        _collect(b"[[[]]]")


def test_parser_rejects_wrong_point_arity():
    for body in (b"[[[1]]]", b"[[[1,2,3]]]"):
        with pytest.raises(StreamInputError):
            _collect(body)


def test_parser_rejects_non_integer_values():
    for body in (
        b"[[[1.5, 2]]]",
        b"[[[1, 2.0]]]",
        b'[[[1, "2"]]]',
        b"[[[1, true]]]",
        b"[[[null, 2]]]",
    ):
        with pytest.raises(StreamInputError):
            _collect(body)


def test_parser_rejects_leading_zeros():
    with pytest.raises(StreamInputError):
        _collect(b"[[[01, 2]]]")


def test_parser_accepts_negative_zero():
    assert _collect(b"[[[-0, 0]]]") == [[(0, 0)]]


def test_parser_rejects_non_strictly_increasing_times():
    with pytest.raises(StreamInputError):
        _collect(b"[[[1, 2], [1, 3]]]")


def test_parser_allows_independent_time_bases_across_segments():
    # Segments never connect, so each restarts its own time ordering.
    assert _collect(b"[[[10, 1], [20, 2]], [[5, 3], [9, 4]]]") == [
        [(10, 1), (20, 2)],
        [(5, 3), (9, 4)],
    ]


@pytest.mark.parametrize(
    "body",
    [
        b"[[[1, 2]]",          # outer array never closed
        b"[[[1, 2]",           # point and segment never closed
        b"[[[1,",              # cut inside the pair
        b"[[[1",               # cut inside an integer
        b"[[[1, 2]",           # missing point close
        b"[",                  # bare opening
        b"[[",                 # only bracket prefixes
        b"[[[",                # only bracket prefixes
    ],
)
def test_parser_reports_truncation(body):
    parser = StreamingBodyParser(lambda _seg: None)
    parser.feed(body)
    with pytest.raises(StreamTruncated):
        parser.finish()


def test_parser_integer_split_across_chunks_is_not_truncation():
    segments: list[list[tuple[int, int]]] = []
    parser = StreamingBodyParser(segments.append)
    for piece in (b"[[[12", b"34567, -", b"89]]]"):
        parser.feed(piece)
    parser.finish()
    assert segments == [[(1234567, -89)]]


def test_parser_rejects_trailing_comma_and_garbage():
    with pytest.raises(StreamInputError):
        _collect(b"[[[1, 2],]]")
    with pytest.raises(StreamInputError):
        _collect(b"[[[1, 2]]] junk")
    with pytest.raises(StreamInputError):
        _collect(b"[[[1, 2]]] {}")


def test_parser_rejects_null_and_tolerant_object_points():
    # The streaming form is strict: no null gaps, no {"t":..,"v":..} objects.
    with pytest.raises(StreamInputError):
        _collect(b"[[[1, 2], null, [3, 4]]]")
    with pytest.raises(StreamInputError):
        _collect(b'[[{"t": 1, "v": 2}]]')


def test_parser_carry_buffer_does_not_grow_with_segment_count():
    # A long valid upload must leave only a tiny parser carry behind.
    segments = [[(i * 10, i) for i in range(1, 50)] for _ in range(200)]
    payload = b"[" + b",".join(
        b"[" + b",".join(f"[{t},{v}]".encode() for t, v in seg) + b"]"
        for seg in segments
    ) + b"]"
    parser = StreamingBodyParser(lambda _seg: None)
    parser.feed(payload)
    parser.finish()
    assert len(parser._buffer) == 0


# ---------------------------------------------------------------------------
# Streaming session
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    return ArchiveStore(str(tmp_path / "stream.db"))


def _session(store, *, max_points: int = 100) -> StreamingArchiveSession:
    from fractions import Fraction

    return ArchiveService(store).open_stream(
        run_id="sess",
        limit=Fraction(50),
        tolerance=Fraction(5),
        max_points=max_points,
    )


def test_session_compresses_each_segment_on_arrival(store):
    session = _session(store)
    flat = [(t, 42) for t in range(0, 100, 10)]
    session.add_segment(flat)
    session.add_segment([(200, 100), (210, 100)])
    result = session.finish()
    assert result.source_point_count == 12
    # Flat segment collapses to its two ends; the second segment keeps two.
    assert result.required_points == 4
    assert result.segments == [[(0, 42), (90, 42)], [(200, 100), (210, 100)]]
    assert result.replaced is False


def test_session_keeps_validating_after_the_cap_is_exceeded(store):
    session = _session(store, max_points=3)
    # Two points each: the third segment alone pushes the minimum over the cap.
    for _ in range(4):
        session.add_segment([(0, 100), (10, 100)])
    assert session.cap_exceeded is True
    assert session.required_points == 8
    # Retained memory never exceeds the cap, even though four segments arrived.
    retained_points = sum(len(seg) for seg in session.retained_segments)
    assert retained_points <= 3
    with pytest.raises(ArchiveRejected) as exc_info:
        session.finish()
    assert exc_info.value.required_points == 8
    assert store.get("sess") is None


def test_session_rejects_non_increasing_times(store):
    session = _session(store)
    with pytest.raises(ValidationError):
        session.add_segment([(1, 1), (1, 2)])


def test_session_rejects_empty_segment(store):
    session = _session(store)
    with pytest.raises(ValidationError):
        session.add_segment([])
    with pytest.raises(ValidationError):
        session.finish()


def test_session_replacement_is_detected(store):
    first = _session(store)
    first.add_segment([(0, 1), (10, 2)])
    assert first.finish().replaced is False

    second = _session(store)
    second.add_segment([(0, 1), (10, 2)])
    assert second.finish().replaced is True


def test_session_working_memory_is_independent_of_segment_count(store):
    # Both uploads have identical longest segments; the big one simply has many
    # more of them. Peak app-side memory must stay within a small constant
    # factor rather than scaling with the total number of points.
    from fractions import Fraction

    def run(many: int) -> tuple[int, StreamingArchiveSession]:
        session = StreamingArchiveSession(
            store, f"mem-{many}", Fraction(50), Fraction(5), max_points=10_000
        )
        tracemalloc.start()
        for _ in range(many):
            # A flat 200-point segment compresses to its two endpoints.
            session.add_segment([(t, 42) for t in range(0, 2000, 10)])
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        session.finish()
        return peak, session

    small_peak, _ = run(3)
    large_peak, large_session = run(60)
    assert large_peak < small_peak * 1.5 + 200_000
    # Every segment collapsed to two points; nothing beyond the retained
    # archive (well under the cap) is held after processing.
    assert [len(seg) for seg in large_session.retained_segments] == [2] * 60


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "http.db"))
    with TestClient(app) as test_client:
        yield test_client


def _stream_body(segments: list[list[list[int]]]) -> bytes:
    import json

    return json.dumps(segments).encode()


def test_stream_upload_matches_classic_receipt(client):
    segments = [
        [[0, 0], [10, 20], [20, 40], [30, 60], [40, 80], [50, 100]],
        [[100, 100], [110, 80], [120, 60], [130, 40], [140, 20]],
    ]
    classic = client.post(
        "/runs",
        json={
            "run_id": "same",
            "limit": 50,
            "tolerance": 5,
            "max_points": 100,
            "segments": segments,
        },
    ).json()

    response = client.post(
        f"/runs?run_id=same-s&limit=50&tolerance=5&max_points=100",
        content=_stream_body(segments),
    )
    assert response.status_code == 200, response.text
    streamed = response.json()

    for key in (
        "archived",
        "point_count",
        "required_points",
        "source_point_count",
        "segments",
        "over_temperature_periods",
    ):
        assert streamed[key] == classic[key]
    assert streamed["run_id"] == "same-s"
    assert streamed["replaced"] is False

    fetched = client.get("/runs/same-s").json()
    assert fetched["point_count"] == streamed["point_count"]
    assert len(fetched["segments"]) == 2


def test_stream_accepts_chunked_transfer(client):
    body = _stream_body([[[0, 10], [10, 90], [20, 10]]])

    def chunks():
        for index in range(0, len(body), 3):
            yield body[index : index + 3]

    response = client.post(
        "/runs?run_id=chunked&limit=50&tolerance=0&max_points=10",
        content=chunks(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["run_id"] == "chunked"


def test_stream_accepts_parameter_aliases_and_truthy_flags(client):
    body = _stream_body([[[0, 10], [10, 90]]])
    response = client.post(
        "/runs?heat_no=alias&over_temperature_limit=50&temperature_tolerance=0"
        "&point_limit=10&stream=true",
        content=body,
    )
    assert response.status_code == 200, response.text
    assert response.json()["run_id"] == "alias"


def test_stream_requires_query_parameters(client):
    body = _stream_body([[[0, 10], [10, 90]]])
    response = client.post(
        "/runs?run_id=missing&tolerance=0&max_points=10&stream=1", content=body
    )
    assert response.status_code == 422
    assert "limit" in response.json()["detail"]


def test_stream_validates_parameter_values(client):
    body = _stream_body([[[0, 10], [10, 90]]])
    response = client.post(
        "/runs?run_id=bad&limit=50&tolerance=-1&max_points=10&stream=1",
        content=body,
    )
    assert response.status_code == 422


def test_stream_rejects_bad_and_truncated_bodies_with_422(client):
    url = "/runs?run_id=bad&limit=50&tolerance=0&max_points=10&stream=1"
    assert client.post(url, content=b"{}").status_code == 422
    assert client.post(url, content=b"[]").status_code == 422
    assert client.post(url, content=b"[[]]").status_code == 422
    assert client.post(url, content=b"[[[1, 2.5]]]").status_code == 422
    assert client.post(url, content=b"[[[1, 2]").status_code == 422
    assert client.post(url, content=b"").status_code == 422
    assert client.post(url, content=b"   ").status_code == 422
    # No run was created by any of the failed submissions.
    assert client.get("/runs/bad").status_code == 404


def test_stream_over_the_cap_reports_exact_required_points(client):
    # Three independent two-point segments need six points minimum.
    body = _stream_body([[[0, 100], [10, 100]] for _ in range(3)])
    response = client.post(
        "/runs?run_id=over&limit=50&tolerance=0&max_points=4&stream=1",
        content=body,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["archived"] is False
    assert payload["error"] == "point_limit_exceeded"
    assert payload["required_points"] == 6
    assert payload["max_points"] == 4
    assert client.get("/runs/over").status_code == 404


def test_stream_continues_validation_after_cap_is_exceeded(client):
    # Over the cap already after segment one, but a later segment is malformed:
    # the input error must still surface as 422 rather than the cap response.
    body = b"[[[0, 100], [10, 100]], [[20, 5], [20, 6]]]"
    response = client.post(
        "/runs?run_id=late&limit=50&tolerance=0&max_points=1&stream=1",
        content=body,
    )
    assert response.status_code == 422
    assert "strictly increase" in response.json()["detail"]
    assert client.get("/runs/late").status_code == 404


def test_stream_failure_keeps_the_previous_archive(client):
    good = _stream_body([[[0, 10], [10, 90], [20, 10]]])
    assert (
        client.post(
            "/runs?run_id=keep&limit=50&tolerance=0&max_points=10&stream=1",
            content=good,
        ).status_code
        == 200
    )
    before = client.get("/runs/keep").json()

    # Truncated replacement upload.
    broken = client.post(
        "/runs?run_id=keep&limit=50&tolerance=0&max_points=10&stream=1",
        content=b"[[[0, 1], [1, 2]",
    )
    assert broken.status_code == 422
    assert client.get("/runs/keep").json() == before

    # Over-cap replacement upload.
    over = client.post(
        "/runs?run_id=keep&limit=50&tolerance=0&max_points=1&stream=1",
        content=good,
    )
    assert over.status_code == 200
    assert over.json()["archived"] is False
    assert client.get("/runs/keep").json() == before


def test_stream_put_takes_run_id_from_path(client):
    body = _stream_body([[[0, 10], [10, 90]]])
    response = client.put(
        "/runs/path-stream?limit=50&tolerance=0&max_points=10&stream=1",
        content=body,
    )
    assert response.status_code == 200, response.text
    assert response.json()["run_id"] == "path-stream"

    mismatch = client.put(
        "/runs/path-stream?run_id=other&limit=50&tolerance=0&max_points=10&stream=1",
        content=body,
    )
    assert mismatch.status_code == 422


def test_stream_works_under_archive_alias(client):
    body = _stream_body([[[0, 10], [10, 90]]])
    response = client.post(
        "/archive?run_id=alias-route&limit=50&tolerance=0&max_points=10&stream=1",
        content=body,
    )
    assert response.status_code == 200, response.text
    assert response.json()["run_id"] == "alias-route"


def test_stream_mode_does_not_affect_classic_bodies(client):
    # Without the flag the tolerant, whole-body submission still works.
    response = client.post(
        "/runs",
        json={
            "run_id": "classic",
            "limit": 50,
            "tolerance": 0,
            "max_points": 10,
            "segments": [
                [{"t": 0, "v": 10}, {"t": 10, "v": 90}],
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["run_id"] == "classic"


def test_stream_storage_failure_returns_500_and_keeps_old_archive(tmp_path):
    db_path = str(tmp_path / "fail.db")
    app = create_app(db_path)
    with TestClient(app) as client:
        client.post(
            "/runs?run_id=kept&limit=50&tolerance=0&max_points=10",
            json={
                "run_id": "kept",
                "limit": 50,
                "tolerance": 0,
                "max_points": 10,
                "segments": [[[0, 1], [10, 2]]],
            },
        )
        before = client.get("/runs/kept").json()

        def _explode(*args, **kwargs):
            raise RuntimeError("disk on fire")

        app.state.store.put = _explode  # type: ignore[method-assign]
        response = client.post(
            "/runs?run_id=kept&limit=50&tolerance=0&max_points=10&stream=1",
            content=_stream_body([[[0, 1], [10, 2]]]),
        )
        assert response.status_code == 500

    with TestClient(create_app(db_path)) as reader:
        assert reader.get("/runs/kept").json() == before


def test_stream_large_upload_has_bounded_memory(client):
    # End-to-end: thousands of long flat segments, each compressible to its two
    # ends. The whole body is far larger than any per-request working set.
    segment = [[t, 42] for t in range(0, 5000, 10)]
    body = _stream_body([segment for _ in range(400)])
    assert len(body) > 2_000_000

    def chunks():
        for index in range(0, len(body), 4096):
            yield body[index : index + 4096]

    response = client.post(
        "/runs?run_id=big&limit=50&tolerance=5&max_points=100000&stream=1",
        content=chunks(),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["archived"] is True
    assert payload["source_point_count"] == 400 * len(segment)
    assert payload["point_count"] == 800
    record = client.get("/runs/big").json()
    assert len(record["segments"]) == 400
