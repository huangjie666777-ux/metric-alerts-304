"""End-to-end: real HTTP server -> scraper -> engine -> SQLite store."""

import asyncio
import time

import aiohttp
import pytest
from aiohttp import web

from app.alerts import AlertEngine
from app.config import RuleConfig, TargetConfig
from app.scraper import TargetScraper
from app.store import Store


class Harness:
    def __init__(self, tmp_path, body=b"# TYPE m gauge\nm 1\n", target_overrides=None):
        self.state = {"body": body, "status": 200, "delay": 0.0}
        self.store = Store(str(tmp_path / "it.db"))
        self.engine = AlertEngine([
            RuleConfig(id="r1", target_id="t1", metric="m", labels={},
                       threshold=10.0, duration_seconds=0.0)
        ])
        tcfg = dict(id="t1", url="", interval_seconds=60.0, timeout_seconds=1.0,
                    max_response_bytes=4096)
        tcfg.update(target_overrides or {})
        self.target = TargetConfig(**tcfg)
        self.session = None
        self.runner = None

    async def __aenter__(self):
        async def metrics(request):
            if self.state["delay"]:
                await asyncio.sleep(self.state["delay"])
            return web.Response(body=self.state["body"], status=self.state["status"])

        app = web.Application()
        app.router.add_get("/metrics", metrics)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        object.__setattr__(self.target, "url", f"http://127.0.0.1:{port}/metrics")
        self.session = aiohttp.ClientSession()
        self.scraper = TargetScraper(self.target, self.session, self.on_round)
        return self

    async def __aexit__(self, *exc):
        await self.session.close()
        await self.runner.cleanup()
        self.store.close()

    def on_round(self, target, result):
        now_mono = time.monotonic()
        now_wall = time.time()
        if result.ok:
            trs = self.engine.evaluate_success(target.id, list(result.samples), now_mono, now_wall)
        else:
            trs = self.engine.evaluate_failure(target.id, now_wall)
        self.store.persist_round(target_id=target.id, ok=result.ok, error=result.error,
                                 samples=result.samples, transitions=trs, ts_wall=now_wall)
        self.engine.apply(trs)
        return result

    async def round(self):
        result = await self.scraper.scrape_once()
        return self.on_round(self.target, result)


def test_successful_round_publishes_samples(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            result = await h.round()
            assert result.ok
            rows = h.store.list_samples("t1")
            assert len(rows) == 1 and rows[0]["metric"] == "m"
            assert h.store.target_health("t1")["last_ok"] is True
    asyncio.run(go())


def test_alert_fires_and_resolves(tmp_path):
    async def go():
        async with Harness(tmp_path, body=b"m 20\n") as h:
            await h.round()
            assert len(h.store.list_active_alerts()) == 1
            h.state["body"] = b"m 5\n"
            await h.round()
            assert h.store.list_active_alerts() == []
            events = h.store.list_events(0, 10)
            assert [e["event"] for e in events] == ["firing", "resolved"]
            assert events[1]["reason"] == "recovered"
    asyncio.run(go())


@pytest.mark.parametrize("body", [
    b"m 1 1710000000000\n",                 # explicit timestamp
    b"m NaN\n",                             # non-finite
    b"m{a=\"1\"} 1\nm{a=\"1\"} 2\n",        # duplicate series
    b"# TYPE m counter\nm 1\n",             # unsupported type
    b"this is not metrics\n",               # garbage
])
def test_bad_payload_fails_round_without_partial_samples(tmp_path, body):
    async def go():
        async with Harness(tmp_path, body=b"m 1\n") as h:
            await h.round()  # good round first
            h.state["body"] = body
            result = await h.round()
            assert not result.ok
            health = h.store.target_health("t1")
            assert health["last_ok"] is False
            # old snapshot intact, nothing partial published
            rows = h.store.list_samples("t1")
            assert len(rows) == 1 and rows[0]["value"] == 1.0
    asyncio.run(go())


def test_http_error_status_fails_round(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            h.state["status"] = 500
            result = await h.round()
            assert not result.ok and "500" in result.error
    asyncio.run(go())


def test_response_size_limit(tmp_path):
    async def go():
        big = b"m 1\n" + b"#" + b"x" * 5000 + b"\n"
        async with Harness(tmp_path, body=big, target_overrides={"max_response_bytes": 100}) as h:
            result = await h.round()
            assert not result.ok and "limit" in result.error
    asyncio.run(go())


def test_timeout_fails_round(tmp_path):
    async def go():
        async with Harness(tmp_path, target_overrides={"timeout_seconds": 0.2}) as h:
            h.state["delay"] = 2.0
            result = await h.round()
            assert not result.ok and "timeout" in result.error
    asyncio.run(go())


def test_connection_refused_fails_round(tmp_path):
    async def go():
        h = Harness(tmp_path)
        h.store = Store(str(tmp_path / "it.db"))
        object.__setattr__(h.target, "url", "http://127.0.0.1:1/metrics")
        async with aiohttp.ClientSession() as session:
            scraper = TargetScraper(h.target, session, h.on_round)
            result = await scraper.scrape_once()
            assert not result.ok and "request failed" in result.error
        h.store.close()
    asyncio.run(go())
