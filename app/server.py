"""Read-only HTTP API for health, samples, active alerts and events."""

from __future__ import annotations

import json

from aiohttp import web

from .alerts import AlertEngine
from .config import AppConfig
from .store import Store

MAX_EVENT_LIMIT = 500


def create_app(cfg: AppConfig, store: Store, engine: AlertEngine) -> web.Application:
    app = web.Application()
    app["cfg"] = cfg
    app["store"] = store
    app["engine"] = engine
    app.router.add_get("/healthz", handle_healthz)
    app.router.add_get("/api/targets", handle_targets)
    app.router.add_get("/api/targets/{target_id}/samples", handle_samples)
    app.router.add_get("/api/alerts", handle_alerts)
    app.router.add_get("/api/events", handle_events)
    return app


async def handle_healthz(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def handle_targets(request: web.Request) -> web.Response:
    cfg: AppConfig = request.app["cfg"]
    store: Store = request.app["store"]
    out = []
    for t in cfg.targets:
        out.append(
            {
                "id": t.id,
                "url": t.url,
                "interval_seconds": t.interval_seconds,
                "timeout_seconds": t.timeout_seconds,
                "max_response_bytes": t.max_response_bytes,
                "health": store.target_health(t.id),
            }
        )
    return web.json_response({"targets": out})


async def handle_samples(request: web.Request) -> web.Response:
    cfg: AppConfig = request.app["cfg"]
    store: Store = request.app["store"]
    target_id = request.match_info["target_id"]
    if target_id not in {t.id for t in cfg.targets}:
        raise web.HTTPNotFound(
            text=json.dumps({"error": f"unknown target {target_id!r}"}),
            content_type="application/json",
        )
    metric = request.query.get("metric") or None
    rows = store.list_samples(target_id, metric)
    return web.json_response(
        {
            "target_id": target_id,
            "samples": [
                {
                    "metric": row["metric"],
                    "labels": json.loads(row["labels_json"]),
                    "value": row["value"],
                }
                for row in rows
            ],
        }
    )


async def handle_alerts(request: web.Request) -> web.Response:
    cfg: AppConfig = request.app["cfg"]
    store: Store = request.app["store"]
    rules = {r.id: r for r in cfg.rules}
    alerts = []
    for row in store.list_active_alerts():
        rule = rules.get(row["rule_id"])
        alerts.append(
            {
                "rule_id": row["rule_id"],
                "target_id": row["target_id"],
                "labels": json.loads(row["labels_json"]),
                "value": row["value"],
                "fired_ts": row["fired_ts"],
                "metric": rule.metric if rule else None,
                "threshold": rule.threshold if rule else None,
                "duration_seconds": rule.duration_seconds if rule else None,
            }
        )
    return web.json_response({"alerts": alerts})


async def handle_events(request: web.Request) -> web.Response:
    store: Store = request.app["store"]
    try:
        after_id = int(request.query.get("after_id", "0"))
        limit = int(request.query.get("limit", "50"))
    except ValueError:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "after_id and limit must be integers"}),
            content_type="application/json",
        )
    if after_id < 0 or not 1 <= limit <= MAX_EVENT_LIMIT:
        raise web.HTTPBadRequest(
            text=json.dumps(
                {"error": f"after_id must be >= 0 and limit between 1 and {MAX_EVENT_LIMIT}"}
            ),
            content_type="application/json",
        )
    rows = store.list_events(after_id, limit)
    events = [
        {
            "id": row["id"],
            "ts": row["ts"],
            "target_id": row["target_id"],
            "rule_id": row["rule_id"],
            "labels": json.loads(row["labels_json"]),
            "event": row["event"],
            "reason": row["reason"],
            "value": row["value"],
        }
        for row in rows
    ]
    return web.json_response(
        {
            "events": events,
            "next_after_id": events[-1]["id"] if events else None,
        }
    )
