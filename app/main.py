"""FastAPI surface for the furnace temperature-curve archive.

Endpoints
---------
``POST /runs`` (aliases ``/archive``, ``/archives``; also under ``/api``)
    Submit one furnace run: segmented samples plus the over-temperature
    ``limit``, ``tolerance`` and ``max_points``.  The smallest valid subset of
    measured points is stored.  If even that exceeds ``max_points`` nothing is
    written, the previous archive (if any) is kept, and the response reports
    ``archived: false`` together with the ``required_points`` that would be
    needed.

``GET /runs/{run_id}``
    Return the archived polyline per segment and the over-temperature periods
    computed from that archived polyline and the archived limit.

``GET /runs``
    List archived runs.

Parameters may travel in the JSON body or as query parameters.  A body that is
a bare list of points is accepted as one segment.  ``PUT /runs/{run_id}`` takes
the run id from the path.
"""

from __future__ import annotations

import json
import os
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .schemas import ValidationError, parse_archive_request
from .service import ArchiveRejected, ArchiveService, ArchiveResult
from .storage import DEFAULT_DB_PATH, ArchiveStore

__all__ = ["create_app", "app"]


_QUERY_KEYS = {
    "run_id": ("run_id", "run", "heat_no", "heat_id", "batch_id"),
    "limit": ("limit", "limit_line", "limit_temperature", "over_temperature_limit"),
    "tolerance": ("tolerance", "temp_tolerance", "temperature_tolerance"),
    "max_points": (
        "max_points",
        "point_limit",
        "max_stored_points",
        "max_archived_points",
    ),
}


def _created(service: ArchiveService, result: ArchiveResult) -> JSONResponse:
    archive = service.get(result.run_id)
    body = {
        "run_id": result.run_id,
        "archived": True,
        "replaced": result.replaced,
        "source_point_count": result.source_point_count,
        "point_count": result.required_points,
        "required_points": result.required_points,
        "segments": [
            {
                "points": [{"t": t, "v": v} for t, v in segment],
                "point_count": len(segment),
            }
            for segment in result.segments
        ],
        "archive": archive,
        "over_temperature_periods": archive["over_temperature_periods"],
    }
    return JSONResponse(status_code=200, content=body)


def _cap_exceeded(exc: ArchiveRejected) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={
            "run_id": exc.run_id,
            "archived": False,
            "error": "point_limit_exceeded",
            "required_points": exc.required_points,
            "point_count": exc.required_points,
            "max_points": exc.max_points,
            "message": (
                "the minimum possible archive still exceeds max_points; "
                "the previous archive (if any) was kept unchanged"
            ),
        },
    )


def _merge_query(payload: Any, request: Request) -> Any:
    """Fill missing top-level parameters from query parameters."""
    if not isinstance(payload, dict):
        payload = {"points": payload}
    merged = dict(payload)
    for canonical, aliases in _QUERY_KEYS.items():
        if any(alias in merged for alias in (canonical, *aliases)):
            continue
        for alias in aliases:
            value = request.query_params.get(alias)
            if value is not None:
                merged[canonical] = value
                break
    return merged


async def _payload(request: Request, body: Any) -> Any:
    if body is not None:
        return body
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=422, detail="request body is required")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"invalid JSON: {exc.msg}") from exc


class ArchiveRequestBody(BaseModel):
    """Documentation-only schema for the archive request body."""

    model_config = ConfigDict(extra="allow")

    run_id: str | int | None = Field(default=None, description="Furnace run id")
    limit: Any = Field(description="Over-temperature line")
    tolerance: Any = Field(description="Allowed temperature deviation")
    max_points: Any = Field(description="Maximum archived points for the run")
    segments: Any = Field(
        default=None,
        description="Segments of [t_ms, temperature] points, or a flat point list",
    )
    points: Any = Field(
        default=None,
        description="Alternative to segments: a flat list of points",
    )


def _request_body_schema() -> dict[str, Any]:
    schema = ArchiveRequestBody.model_json_schema()
    schema.pop("title", None)
    return {
        "required": True,
        "content": {"application/json": {"schema": schema}},
    }


def create_app(db_path: str | None = None) -> FastAPI:
    app = FastAPI(
        title="Furnace Temperature Archive",
        version="1.0.0",
        description=(
            "Compresses furnace temperature curves into minimal archives while "
            "preserving the tolerance band and the exact over-temperature periods."
        ),
    )
    store = ArchiveStore(db_path or DEFAULT_DB_PATH)
    service = ArchiveService(store)
    app.state.store = store
    app.state.service = service

    def _store(payload: Any, path_run_id: str | None, request: Request) -> JSONResponse:
        try:
            merged = _merge_query(payload, request)
            run = parse_archive_request(merged, path_run_id)
            result = service.archive(run)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ArchiveRejected as exc:
            return _cap_exceeded(exc)
        return _created(service, result)

    def _get(run_id: str) -> dict[str, Any]:
        record = service.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="run not found")
        return record

    @app.get("/health")
    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/runs")
    @app.post("/archive")
    @app.post("/archives")
    @app.post("/api/runs")
    @app.post("/api/archive")
    @app.post("/api/archives")
    async def archive_run(
        request: Request, body: Any = Body(default=None)
    ) -> JSONResponse:
        return _store(await _payload(request, body), None, request)

    @app.put("/runs/{run_id}")
    @app.put("/archive/{run_id}")
    @app.put("/archives/{run_id}")
    @app.put("/api/runs/{run_id}")
    async def archive_run_at_path(
        run_id: str, request: Request, body: Any = Body(default=None)
    ) -> JSONResponse:
        return _store(await _payload(request, body), run_id, request)

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        for path, methods in schema["paths"].items():
            if path in (
                "/runs",
                "/archive",
                "/archives",
                "/api/runs",
                "/api/archive",
                "/api/archives",
            ):
                methods["post"]["requestBody"] = _request_body_schema()
            elif path.endswith("/{run_id}") and "put" in methods:
                methods["put"]["requestBody"] = _request_body_schema()
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi

    @app.get("/runs")
    @app.get("/api/runs")
    def list_runs() -> dict[str, object]:
        return {"runs": service.list()}

    @app.get("/runs/{run_id}")
    @app.get("/archive/{run_id}")
    @app.get("/archives/{run_id}")
    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        return _get(run_id)

    return app


app = create_app(os.environ.get("FURNACE_DB_PATH"))
