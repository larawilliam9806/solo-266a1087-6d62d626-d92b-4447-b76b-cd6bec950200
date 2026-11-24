"""Request parsing and semantic validation.

The HTTP surface is deliberately tolerant about spelling so QC systems can post
whatever their export looks like, while the internal model is strict: integer
milliseconds, integer temperatures and exact rational parameters.

Accepted request shapes (top level may be an object; a bare list is treated as
``{"points": [...]}``)::

    {
      "run_id": "H-2026-001",              # or run / heat_no / heat_id / batch_id
      "limit": 217, "tolerance": 2, "max_points": 200,
      "segments": [
        [[0, 25], [500, 180], [1000, 217]],
        [[1200, 220], [1800, 240]]
      ]
    }

Points may be ``[t_ms, temp]`` pairs or objects such as
``{"t": 0, "v": 25}`` / ``{"time_ms": 0, "temperature": 25}``.  Segments may
also be ``{"points": [...]}``, ``{"times": [...], "values": [...]}`` or a flat
list of points (a single segment).  A ``null`` entry -- or a point whose
temperature is ``null`` -- marks a missing measurement; the curve is broken
there and never drawn across the gap.

``limit``, ``tolerance`` and ``max_points`` may also live in a nested
``parameters``/``params``/``config`` object.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable

from .compression import to_fraction

__all__ = [
    "ParsedRun",
    "parse_archive_request",
    "ValidationError",
]


class ValidationError(ValueError):
    """Raised for request bodies that fail semantic validation."""


@dataclass(frozen=True)
class ParsedRun:
    run_id: str
    segments: tuple[tuple[tuple[int, int], ...], ...]
    limit: Fraction
    tolerance: Fraction
    max_points: int

    @property
    def point_count(self) -> int:
        return sum(len(segment) for segment in self.segments)


_RUN_KEYS = (
    "run_id",
    "run",
    "run_no",
    "run_number",
    "furnace_run",
    "furnace_run_id",
    "heat_id",
    "heat_no",
    "heat_number",
    "heat",
    "batch_id",
    "batch_no",
    "id",
)
_LIMIT_KEYS = (
    "limit",
    "limit_line",
    "limit_temperature",
    "limit_temp",
    "over_temperature_limit",
    "over_temperature_line",
    "over_temperature_threshold",
    "overtemp_limit",
    "overtemp_line",
    "over_temp_line",
    "temperature_limit",
    "threshold",
)
_TOLERANCE_KEYS = (
    "tolerance",
    "temp_tolerance",
    "temperature_tolerance",
    "allowed_tolerance",
    "allowed_deviation",
    "deviation",
    "error_tolerance",
)
_MAX_POINTS_KEYS = (
    "max_points",
    "point_limit",
    "points_limit",
    "max_stored_points",
    "max_archived_points",
    "max_samples",
    "storage_limit",
    "capacity",
)
_SEGMENT_KEYS = ("segments", "segmented_samples", "curves", "runs")
_POINT_KEYS = ("points", "samples", "data", "series", "polyline", "curve")
_PARAM_GROUP_KEYS = ("parameters", "params", "config", "settings", "options")

_TIME_KEYS = (
    "t",
    "time",
    "t_ms",
    "time_ms",
    "ms",
    "milliseconds",
    "timestamp_ms",
    "timestamp",
)
_VALUE_KEYS = (
    "v",
    "temp",
    "temperature",
    "value",
    "temp_c",
    "temperature_c",
    "temperature_value",
    "celsius",
)
_GROUP_KEYS = ("points", "samples", "data", "values", "series")
_TIMES_KEYS = ("times", "t_ms", "timestamps_ms", "time_ms", "t", "timestamps")
_VALUES_KEYS = ("values", "temps", "temperatures", "v", "temperature", "readings")


def _normalise_key(key: str) -> str:
    # ``OverTemperatureLimit`` -> ``over_temperature_limit``,
    # ``max-points`` / ``Max Points`` -> ``max_points``.
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key.strip())
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", spaced)
    return re.sub(r"[\s\-]+", "_", spaced).lower()


def _find_key(raw: dict[str, Any], keys: Iterable[str]) -> str | None:
    """Find a key by exact name first, then by case/separator-insensitive match."""
    for key in keys:
        if key in raw:
            return key
    wanted = {_normalise_key(key) for key in keys}
    for key in raw:
        if isinstance(key, str) and _normalise_key(key) in wanted:
            return key
    return None


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (int, float, str)) and not isinstance(value, bool)


def _as_integer(value: Any, what: str) -> int:
    if isinstance(value, bool):
        raise ValidationError(f"{what} must be an integer, not a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ValidationError(f"{what} must be an integer, got {value!r}")
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError as exc:
            raise ValidationError(f"{what} must be an integer: {value!r}") from exc
    raise ValidationError(f"{what} must be an integer: {value!r}")


def _is_point_like(raw: Any) -> bool:
    if isinstance(raw, (list, tuple)):
        return len(raw) == 2 and all(_is_scalar(item) for item in raw)
    if isinstance(raw, dict):
        tk = _find_key(raw, _TIME_KEYS)
        vk = _find_key(raw, _VALUE_KEYS)
        return (
            tk is not None
            and vk is not None
            and _is_scalar(raw[tk])
            and _is_scalar(raw[vk])
        )
    return False


def _point_is_missing(raw: Any) -> bool:
    if raw is None:
        return True
    if isinstance(raw, dict):
        if raw.get("missing") is True or raw.get("is_missing") is True:
            return True
        tk = _find_key(raw, _TIME_KEYS)
        vk = _find_key(raw, _VALUE_KEYS)
        if tk is not None and raw.get(tk) is None:
            return True
        if vk is not None and raw.get(vk) is None:
            return True
    return False


def _parse_point(raw: Any, where: str) -> tuple[int, int]:
    if isinstance(raw, dict):
        tk = _find_key(raw, _TIME_KEYS)
        vk = _find_key(raw, _VALUE_KEYS)
        if tk is None or vk is None:
            raise ValidationError(
                f"{where}: point object needs a millisecond and a temperature key"
            )
        return _as_integer(raw[tk], f"{where}: milliseconds"), _as_integer(
            raw[vk], f"{where}: temperature"
        )
    if isinstance(raw, (list, tuple)):
        if len(raw) != 2:
            raise ValidationError(f"{where}: a point pair needs exactly two values")
        return _as_integer(raw[0], f"{where}: milliseconds"), _as_integer(
            raw[1], f"{where}: temperature"
        )
    raise ValidationError(f"{where}: unsupported point {raw!r}")


def _parse_point_list(raw: Any, where: str) -> list[list[tuple[int, int]]]:
    """Parse a flat point list, splitting into segments at missing samples."""
    if not isinstance(raw, (list, tuple)):
        raise ValidationError(f"{where}: expected a list of points")
    groups: list[list[tuple[int, int]]] = [[]]
    for i, item in enumerate(raw):
        if _point_is_missing(item):
            if groups[-1]:
                groups.append([])
            continue
        groups[-1].append(_parse_point(item, f"{where}[{i}]"))
    return [group for group in groups if group]


def _parse_segment_arrays(raw: dict[str, Any], where: str) -> list[tuple[int, int]]:
    tk = _find_key(raw, _TIMES_KEYS)
    vk = _find_key(raw, _VALUES_KEYS)
    assert tk is not None and vk is not None
    times, values = raw[tk], raw[vk]
    if not isinstance(times, (list, tuple)) or not isinstance(values, (list, tuple)):
        raise ValidationError(f"{where}: times and values must be lists")
    if len(times) != len(values):
        raise ValidationError(f"{where}: times and values length differ")
    return [
        (
            _as_integer(t, f"{where}: milliseconds"),
            _as_integer(v, f"{where}: temperature"),
        )
        for t, v in zip(times, values)
    ]


def _parse_segment(raw: Any, where: str) -> list[list[tuple[int, int]]]:
    if isinstance(raw, dict):
        # Time/value arrays take precedence: ``values`` would otherwise be
        # mistaken for a generic list-of-points group key.
        if _find_key(raw, _TIMES_KEYS) is not None and _find_key(
            raw, _VALUES_KEYS
        ) is not None:
            points = _parse_segment_arrays(raw, where)
            return [points] if points else []
        gk = _find_key(raw, _GROUP_KEYS)
        if gk is not None:
            return _parse_point_list(raw[gk], where)
        raise ValidationError(f"{where}: cannot find points in segment object")
    if _is_point_like(raw):
        return [[_parse_point(raw, where)]]
    return _parse_point_list(raw, where)


def _parse_segments(value: Any) -> tuple[tuple[tuple[int, int], ...], ...]:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)) or len(value) == 0:
        raise ValidationError("segments must be a non-empty list")

    if all(_is_point_like(item) or _point_is_missing(item) for item in value):
        groups = _parse_point_list(value, "segments")
    else:
        groups = []
        for i, item in enumerate(value):
            groups.extend(_parse_segment(item, f"segments[{i}]"))

    if not groups:
        raise ValidationError("at least one non-empty segment is required")

    result: list[tuple[tuple[int, int], ...]] = []
    for i, points in enumerate(groups):
        if not points:
            raise ValidationError(f"segments[{i}] must contain at least one point")
        for k, (t, _v) in enumerate(points):
            if k and t <= points[k - 1][0]:
                raise ValidationError(
                    f"segments[{i}] sample times must strictly increase "
                    f"(index {k}: {t} after {points[k - 1][0]})"
                )
        result.append(tuple(points))
    return tuple(result)


def _parse_run_id(raw: Any) -> str:
    if raw is None:
        raise ValidationError("run_id is required")
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise ValidationError("run_id must be a string or integer")
    run_id = str(raw).strip()
    if not run_id:
        raise ValidationError("run_id must not be empty")
    return run_id


def _parse_limit(raw: Any) -> Fraction:
    try:
        return to_fraction(raw)
    except (ValueError, ZeroDivisionError, TypeError) as exc:
        raise ValidationError(f"limit must be a finite number: {raw!r}") from exc


def _parse_tolerance(raw: Any) -> Fraction:
    try:
        tolerance = to_fraction(raw)
    except (ValueError, ZeroDivisionError, TypeError) as exc:
        raise ValidationError(
            f"tolerance must be a finite number: {raw!r}"
        ) from exc
    if tolerance < 0:
        raise ValidationError("tolerance must be non-negative")
    return tolerance


def _parse_max_points(raw: Any) -> int:
    value = _as_integer(raw, "max_points")
    if value < 0:
        raise ValidationError("max_points must be non-negative")
    return value


def _lookup(
    body: dict[str, Any], keys: Iterable[str], nested: dict[str, Any] | None
) -> Any:
    key = _find_key(body, keys)
    if key is not None:
        return body[key]
    if nested is not None:
        key = _find_key(nested, keys)
        if key is not None:
            return nested[key]
    return None



def parse_archive_request(
    payload: Any, path_run_id: str | None = None
) -> ParsedRun:
    if isinstance(payload, (list, tuple)):
        payload = {"points": payload}
    if not isinstance(payload, dict):
        raise ValidationError("request body must be a JSON object or list")

    nested = None
    group_key = _find_key(payload, _PARAM_GROUP_KEYS)
    if group_key is not None and isinstance(payload[group_key], dict):
        nested = payload[group_key]

    if path_run_id is not None:
        run_id = _parse_run_id(path_run_id)
        body_run_id = _lookup(payload, _RUN_KEYS, nested)
        if body_run_id is not None and str(body_run_id).strip() != run_id:
            raise ValidationError(
                "run_id in the body does not match the run_id in the path"
            )
    else:
        run_id = _parse_run_id(_lookup(payload, _RUN_KEYS, nested))

    segments_raw = _lookup(payload, _SEGMENT_KEYS, nested)
    if segments_raw is None:
        segments_raw = _lookup(payload, _POINT_KEYS, nested)
    if segments_raw is None:
        raise ValidationError("segments (or points) are required")
    segments = _parse_segments(segments_raw)

    limit_raw = _lookup(payload, _LIMIT_KEYS, nested)
    if limit_raw is None:
        raise ValidationError("limit is required")
    tolerance_raw = _lookup(payload, _TOLERANCE_KEYS, nested)
    if tolerance_raw is None:
        raise ValidationError("tolerance is required")
    max_points_raw = _lookup(payload, _MAX_POINTS_KEYS, nested)
    if max_points_raw is None:
        raise ValidationError("max_points is required")

    return ParsedRun(
        run_id=run_id,
        segments=segments,
        limit=_parse_limit(limit_raw),
        tolerance=_parse_tolerance(tolerance_raw),
        max_points=_parse_max_points(max_points_raw),
    )
