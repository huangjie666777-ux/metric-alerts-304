"""Application wiring and lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import time

import aiohttp
from aiohttp import web

from .alerts import REASON_RESTART, AlertEngine
from .config import AppConfig, ConfigError, load_config
from .scraper import ScrapeResult, TargetScraper
from .server import create_app
from .store import Store

log = logging.getLogger("app")


async def run_server(cfg: AppConfig) -> None:
    store = Store(cfg.sqlite_path)
    restarted = store.resolve_firing_on_restart(REASON_RESTART, time.time())
    if restarted:
        log.info(
            "resolved %d alert(s) left firing by a previous run (reason=%s)",
            len(restarted),
            REASON_RESTART,
        )
    engine = AlertEngine(cfg.rules)

    def on_round(target, result: ScrapeResult) -> None:
        now_mono = time.monotonic()
        now_wall = time.time()
        if result.ok:
            transitions = engine.evaluate_success(
                target.id, list(result.samples), now_mono, now_wall
            )
        else:
            transitions = engine.evaluate_failure(target.id, now_wall)
            log.warning("scrape failed for target %s: %s", target.id, result.error)
        store.persist_round(
            target_id=target.id,
            ok=result.ok,
            error=result.error,
            samples=result.samples,
            transitions=transitions,
            ts_wall=now_wall,
        )
        engine.apply(transitions)
        for tr in transitions:
            for ev in tr.events:
                log.info(
                    "event: %s rule=%s target=%s labels=%s reason=%s value=%s",
                    ev.event, ev.rule_id, ev.target_id, ev.labels_json,
                    ev.reason, ev.value,
                )

    session = aiohttp.ClientSession()
    runner = web.AppRunner(create_app(cfg, store, engine))
    await runner.setup()
    site = web.TCPSite(runner, cfg.host, cfg.port)
    await site.start()

    tasks = [
        asyncio.create_task(
            TargetScraper(target, session, on_round).run(),
            name=f"scraper-{target.id}",
        )
        for target in cfg.targets
    ]

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info(
        "listening on http://%s:%d (%d target(s), %d rule(s))",
        cfg.host, cfg.port, len(cfg.targets), len(cfg.rules),
    )
    try:
        await stop.wait()
    finally:
        log.info("shutting down: stopping scrapers")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runner.cleanup()
        await session.close()
        store.close()
        log.info("shutdown complete")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="app",
        description="Metrics scraping and sustained-threshold alerting backend",
    )
    parser.add_argument(
        "--config", default="config.json", help="path to the JSON config file"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        log.error("invalid configuration: %s", exc)
        return 2

    try:
        asyncio.run(run_server(cfg))
    except KeyboardInterrupt:
        pass
    return 0
