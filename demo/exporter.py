"""Demo metrics exporter used by the README walkthrough.

Serves a single gauge `demo_temperature_celsius{room="server"}` on
/metrics (rendered with prometheus-client) and lets you change its value
via GET/POST /set/{value} so alert triggering and recovery can be
demonstrated with curl.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp import web
from prometheus_client import CollectorRegistry, Gauge, generate_latest

registry = CollectorRegistry()
temperature = Gauge(
    "demo_temperature_celsius",
    "Simulated temperature for the alerting demo",
    ["room"],
    registry=registry,
)
state = {"value": 21.0}


async def metrics(request: web.Request) -> web.Response:
    temperature.labels(room="server").set(state["value"])
    return web.Response(body=generate_latest(registry), content_type="text/plain")


async def set_value(request: web.Request) -> web.Response:
    try:
        state["value"] = float(request.match_info["value"])
    except ValueError:
        raise web.HTTPBadRequest(text='{"error": "value must be a number"}',
                                 content_type="application/json")
    return web.json_response({"value": state["value"]})


def main() -> None:
    port = int(os.environ.get("DEMO_PORT", "9100"))
    app = web.Application()
    app.router.add_get("/metrics", metrics)
    app.router.add_get("/set/{value}", set_value)
    app.router.add_post("/set/{value}", set_value)
    print(f"demo exporter on http://127.0.0.1:{port}/metrics")
    web.run_app(app, host="127.0.0.1", port=port, print=None)


if __name__ == "__main__":
    main()
