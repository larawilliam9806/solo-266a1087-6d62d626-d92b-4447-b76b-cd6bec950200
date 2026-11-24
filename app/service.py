"""Business logic: compress a run, enforce the storage cap, persist, and read back."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .compression import (
    above_limit_intervals,
    compress_segment,
    fraction_to_json_number,
)
from .schemas import ParsedRun
from .storage import ArchiveStore

__all__ = ["ArchiveService", "ArchiveRejected", "ArchiveResult"]


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


class ArchiveService:
    def __init__(self, store: ArchiveStore) -> None:
        self.store = store

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
