"""Periodic concurrent scraping.

Each target gets its own asyncio task, so targets never block each other.
Within a target the loop is strictly sequential: a new round only starts
after the previous one finished, so rounds of one target never overlap.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import aiohttp

from .config import TargetConfig
from .textparse import ParseError, Sample, parse

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScrapeResult:
    ok: bool
    samples: tuple[Sample, ...]
    error: str | None


# Called with (target, result) once per finished round.
RoundCallback = Callable[[TargetConfig, ScrapeResult], None]


class TargetScraper:
    def __init__(
        self,
        target: TargetConfig,
        session: aiohttp.ClientSession,
        on_round: RoundCallback,
    ):
        self._target = target
        self._session = session
        self._on_round = on_round

    async def run(self) -> None:
        interval = self._target.interval_seconds
        next_at = time.monotonic()
        while True:
            result = await self.scrape_once()
            try:
                self._on_round(self._target, result)
            except Exception:
                log.exception("failed to process round for target %s", self._target.id)
            next_at += interval
            delay = next_at - time.monotonic()
            if delay < 0:
                # The round took longer than the interval; do not try to
                # catch up with a burst, just continue immediately.
                next_at = time.monotonic()
                delay = 0.0
            await asyncio.sleep(delay)

    async def scrape_once(self) -> ScrapeResult:
        target = self._target
        timeout = aiohttp.ClientTimeout(total=target.timeout_seconds)
        try:
            async with self._session.get(target.url, timeout=timeout) as resp:
                if resp.status != 200:
                    return _fail(f"unexpected HTTP status {resp.status}")
                limit = target.max_response_bytes
                if resp.content_length is not None and resp.content_length > limit:
                    return _fail(
                        f"response too large: {resp.content_length} bytes > limit {limit}"
                    )
                data = await resp.content.read(limit + 1)
                if len(data) > limit:
                    return _fail(f"response exceeds limit of {limit} bytes")
        except TimeoutError:
            return _fail(f"timeout after {target.timeout_seconds}s")
        except aiohttp.ClientError as exc:
            return _fail(f"request failed: {exc}")

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            return _fail(f"response is not valid UTF-8: {exc}")
        try:
            samples = parse(text)
        except ParseError as exc:
            return _fail(f"parse error: {exc}")
        return ScrapeResult(ok=True, samples=tuple(samples), error=None)


def _fail(error: str) -> ScrapeResult:
    return ScrapeResult(ok=False, samples=(), error=error)
