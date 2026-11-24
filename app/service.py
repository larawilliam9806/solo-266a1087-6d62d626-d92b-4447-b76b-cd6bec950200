"""Business logic: compress a run, enforce the storage cap, persist, and read back."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Sequence

from .compression import (
    above_limit_intervals,
    compress_segment,
    fraction_to_json_number,
)
from .schemas import ParsedRun, ValidationError
from .storage import ArchiveStore

__all__ = [
    "ArchiveService",
    "ArchiveRejected",
    "ArchiveResult",
    "StreamingArchiveSession",
]


@dataclass(frozen=True)
class ArchiveResult:
    run_id: str
    segments: list[list[tuple[int, int]]]
    source_point_count: int
    required_points: int
    replaced: bool


class ArchiveRejected(Exception):
    """The compressed archive would still exceed the point cap."""

    def __init__(self, run_id: str, required_points: int, max_points: int) -> None:
        super().__init__(
            f"run {run_id!r} needs {required_points} points, "
            f"which exceeds max_points={max_points}"
        )
        self.run_id = run_id
        self.required_points = required_points
        self.max_points = max_points


def _point_json(t: int, v: int) -> dict[str, int]:
    return {"t": t, "v": v}


class StreamingArchiveSession:
    """Accumulator for a chunked ``stream=1`` submission.

    Segments are fed one at a time and compressed immediately with the same
    :func:`~app.compression.compress_segment` rule as the non-streaming path.
    Raw samples therefore live only for the duration of one :meth:`add_segment`
    call and are released before the next chunk is read.

    Memory bounds
    -------------
    The only per-submission state that can grow is:

    * the segment currently being parsed (the longest single segment), and
    * the retained archive, which never holds more than ``max_points`` points:
      once the running minimum exceeds the cap, later compressed segments are
      counted but not retained.

    So the working memory is O(longest segment + max_points), no matter how
    many segments the upload contains or how far over the cap it runs.
    Validation and the required-point tally still run for every remaining
    segment after the cap is exceeded, so an over-cap response reports the
    exact minimum for the whole run.
    """

    def __init__(
        self,
        store: ArchiveStore,
        run_id: str,
        limit: Fraction,
        tolerance: Fraction,
        max_points: int,
    ) -> None:
        self._store = store
        self.run_id = run_id
        self._limit = limit
        self._tolerance = tolerance
        self._max_points = max_points
        self._retained: list[list[tuple[int, int]]] = []
        self._required_points = 0
        self._source_point_count = 0
        self._segment_count = 0
        self._cap_exceeded = False

    @property
    def required_points(self) -> int:
        """Minimum archive size seen so far, across all received segments."""
        return self._required_points

    @property
    def source_point_count(self) -> int:
        return self._source_point_count

    @property
    def retained_segments(self) -> list[list[tuple[int, int]]]:
        """Archived segments retained so far (only while within the cap)."""
        return self._retained

    @property
    def cap_exceeded(self) -> bool:
        return self._cap_exceeded

    def add_segment(self, points: Sequence[tuple[int, int]]) -> None:
        """Compress one received segment under the run's limit and tolerance."""
        if not points:
            raise ValidationError("each segment must contain at least one point")
        segment_index = self._segment_count
        previous_time: int | None = None
        for time_ms, _value in points:
            if previous_time is not None and time_ms <= previous_time:
                raise ValidationError(
                    f"segments[{segment_index}] sample times must strictly "
                    f"increase (got {time_ms} after {previous_time})"
                )
            previous_time = time_ms

        times = [point[0] for point in points]
        values = [point[1] for point in points]
        try:
            keep = compress_segment(times, values, self._limit, self._tolerance)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        compressed = [points[k] for k in keep]

        self._segment_count += 1
        self._source_point_count += len(points)
        self._required_points += len(compressed)
        if self._required_points > self._max_points:
            # Keep tallying the remaining segments, but do not retain their
            # points: the archive will be rejected anyway and nothing here may
            # grow beyond the configured cap.
            self._cap_exceeded = True
        elif not self._cap_exceeded:
            self._retained.append(compressed)

    def finish(self) -> ArchiveResult:
        """Finalise the run: reject over-cap uploads, otherwise persist atomically.

        On rejection or a storage failure nothing is written, so any previous
        archive for the run stays retrievable unchanged.
        """
        if self._segment_count == 0:
            raise ValidationError("at least one non-empty segment is required")
        if self._cap_exceeded or self._required_points > self._max_points:
            raise ArchiveRejected(
                self.run_id, self._required_points, self._max_points
            )

        replaced = self._store.get(self.run_id) is not None
        self._store.put(
            run_id=self.run_id,
            segments=[
                [_point_json(t, v) for (t, v) in segment]
                for segment in self._retained
            ],
            limit_value=str(self._limit),
            tolerance=str(self._tolerance),
            max_points=self._max_points,
            point_count=self._required_points,
            source_point_count=self._source_point_count,
        )
        return ArchiveResult(
            run_id=self.run_id,
            segments=self._retained,
            source_point_count=self._source_point_count,
            required_points=self._required_points,
            replaced=replaced,
        )


class ArchiveService:
    def __init__(self, store: ArchiveStore) -> None:
        self.store = store

    def open_stream(
        self,
        run_id: str,
        limit: Fraction,
        tolerance: Fraction,
        max_points: int,
    ) -> StreamingArchiveSession:
        """Begin a chunked ``stream=1`` archive submission."""
        return StreamingArchiveSession(
            self.store, run_id, limit, tolerance, max_points
        )

    # -- write ---------------------------------------------------------

    def archive(self, run: ParsedRun) -> ArchiveResult:
        """Compress and store ``run``.

        If the minimum archive is larger than ``run.max_points`` nothing is
        written and :class:`ArchiveRejected` is raised, so a previously stored
        archive for the same run is left untouched.
        """
        archived: list[list[tuple[int, int]]] = []
        for segment in run.segments:
            times = [p[0] for p in segment]
            values = [p[1] for p in segment]
            keep = compress_segment(times, values, run.limit, run.tolerance)
            archived.append([segment[k] for k in keep])

        required_points = sum(len(seg) for seg in archived)
        if required_points > run.max_points:
            raise ArchiveRejected(run.run_id, required_points, run.max_points)

        replaced = self.store.get(run.run_id) is not None
        self.store.put(
            run_id=run.run_id,
            segments=[
                [_point_json(t, v) for (t, v) in segment] for segment in archived
            ],
            limit_value=str(run.limit),
            tolerance=str(run.tolerance),
            max_points=run.max_points,
            point_count=required_points,
            source_point_count=run.point_count,
        )
        return ArchiveResult(
            run_id=run.run_id,
            segments=archived,
            source_point_count=run.point_count,
            required_points=required_points,
            replaced=replaced,
        )

    # -- read ----------------------------------------------------------

    def get(self, run_id: str) -> dict[str, Any] | None:
        record = self.store.get(run_id)
        if record is None:
            return None
        return self._decorate(record)

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "run_id": record["run_id"],
                "point_count": record["point_count"],
                "max_points": record["max_points"],
                "limit": fraction_to_json_number(Fraction(record["limit"])),
                "tolerance": fraction_to_json_number(
                    Fraction(record["tolerance"])
                ),
                "created_at": record["created_at"],
                "updated_at": record["updated_at"],
            }
            for record in self.store.list_runs()
        ]

    @staticmethod
    def _decorate(record: dict[str, Any]) -> dict[str, Any]:
        limit = Fraction(record["limit"])
        tolerance = Fraction(record["tolerance"])

        segments: list[dict[str, Any]] = []
        period_details: list[tuple[Fraction, Fraction, bool, bool, int]] = []
        for index, segment in enumerate(record["segments"]):
            times = [int(p["t"]) for p in segment]
            values = [int(p["v"]) for p in segment]
            segments.append(
                {
                    "index": index,
                    "points": [{"t": t, "v": v} for t, v in zip(times, values)],
                    "point_count": len(times),
                    "start_ms": times[0],
                    "end_ms": times[-1],
                }
            )
            for interval in above_limit_intervals(times, values, limit):
                period_details.append(
                    (
                        interval.start,
                        interval.end,
                        interval.start_closed,
                        interval.end_closed,
                        index,
                    )
                )

        period_details.sort(key=lambda item: (item[0], item[1], item[4]))
        over_periods = [
            {
                "segment_index": index,
                "start_ms": fraction_to_json_number(start),
                "end_ms": fraction_to_json_number(end),
                "duration_ms": fraction_to_json_number(end - start),
                "start_closed": start_closed,
                "end_closed": end_closed,
            }
            for start, end, start_closed, end_closed, index in period_details
        ]
        total: Fraction = sum(
            (end - start for start, end, *_ in period_details), Fraction(0)
        )

        return {
            "run_id": record["run_id"],
            "segments": segments,
            "limit": fraction_to_json_number(limit),
            "tolerance": fraction_to_json_number(tolerance),
            "max_points": record["max_points"],
            "point_count": record["point_count"],
            "source_point_count": record["source_point_count"],
            "over_temperature_periods": over_periods,
            "total_over_temperature_ms": fraction_to_json_number(total),
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
        }
