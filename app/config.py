"""JSON configuration loading and startup validation."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class ConfigError(ValueError):
    """The configuration file is invalid."""


_METRIC_RE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")

_TOP_LEVEL_KEYS = {"host", "port", "sqlite_path", "targets", "rules"}
_TARGET_KEYS = {"id", "url", "interval_seconds", "timeout_seconds", "max_response_bytes"}
_RULE_KEYS = {"id", "target_id", "metric", "labels", "threshold", "duration_seconds"}


@dataclass(frozen=True)
class TargetConfig:
    id: str
    url: str
    interval_seconds: float
    timeout_seconds: float
    max_response_bytes: int


@dataclass(frozen=True)
class RuleConfig:
    id: str
    target_id: str
    metric: str
    labels: dict[str, str]
    threshold: float
    duration_seconds: float


@dataclass(frozen=True)
class AppConfig:
    host: str
    port: int
    sqlite_path: str
    targets: tuple[TargetConfig, ...]
    rules: tuple[RuleConfig, ...]


def load_config(path: str | Path) -> AppConfig:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {p}: {exc}") from exc
    try:
        raw = json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {p}: {exc}") from exc
    return parse_config(raw)


def _reject_constant(token: str) -> None:
    raise ConfigError(f"non-finite literal {token!r} is not allowed in the config")


def parse_config(raw: object) -> AppConfig:
    if not isinstance(raw, dict):
        raise ConfigError("top level must be a JSON object")
    _check_keys(raw, _TOP_LEVEL_KEYS, "config")

    host = raw.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host:
        raise ConfigError("host: must be a non-empty string")

    port = raw.get("port")
    if not _is_int(port) or not 1 <= port <= 65535:
        raise ConfigError("port: must be an integer between 1 and 65535")

    sqlite_path = raw.get("sqlite_path")
    if not isinstance(sqlite_path, str) or not sqlite_path:
        raise ConfigError("sqlite_path: must be a non-empty string")

    raw_targets = raw.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ConfigError("targets: must be a non-empty array")
    targets = tuple(_parse_target(t, i) for i, t in enumerate(raw_targets))
    _check_unique([t.id for t in targets], "target id")

    raw_rules = raw.get("rules")
    if not isinstance(raw_rules, list):
        raise ConfigError("rules: must be an array")
    rules = tuple(_parse_rule(r, i) for i, r in enumerate(raw_rules))
    _check_unique([r.id for r in rules], "rule id")

    target_ids = {t.id for t in targets}
    for rule in rules:
        if rule.target_id not in target_ids:
            raise ConfigError(f"rule {rule.id!r}: unknown target_id {rule.target_id!r}")

    return AppConfig(
        host=host,
        port=port,
        sqlite_path=sqlite_path,
        targets=targets,
        rules=rules,
    )


def _parse_target(raw: object, index: int) -> TargetConfig:
    where = f"targets[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: must be an object")
    _check_keys(raw, _TARGET_KEYS, where)

    tid = raw.get("id")
    if not isinstance(tid, str) or not tid:
        raise ConfigError(f"{where}.id: must be a non-empty string")

    url = raw.get("url")
    if not isinstance(url, str):
        raise ConfigError(f"{where}.url: must be a string")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"{where}.url: must be an absolute http(s) URL, got {url!r}")

    interval = _number(raw.get("interval_seconds"), f"{where}.interval_seconds")
    if interval <= 0:
        raise ConfigError(f"{where}.interval_seconds: must be > 0")

    timeout = _number(raw.get("timeout_seconds"), f"{where}.timeout_seconds")
    if timeout <= 0:
        raise ConfigError(f"{where}.timeout_seconds: must be > 0")

    max_bytes = raw.get("max_response_bytes")
    if not _is_int(max_bytes) or max_bytes <= 0:
        raise ConfigError(f"{where}.max_response_bytes: must be a positive integer")

    return TargetConfig(
        id=tid,
        url=url,
        interval_seconds=float(interval),
        timeout_seconds=float(timeout),
        max_response_bytes=max_bytes,
    )


def _parse_rule(raw: object, index: int) -> RuleConfig:
    where = f"rules[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: must be an object")
    _check_keys(raw, _RULE_KEYS, where)

    rid = raw.get("id")
    if not isinstance(rid, str) or not rid:
        raise ConfigError(f"{where}.id: must be a non-empty string")

    target_id = raw.get("target_id")
    if not isinstance(target_id, str) or not target_id:
        raise ConfigError(f"{where}.target_id: must be a non-empty string")

    metric = raw.get("metric")
    if not isinstance(metric, str) or not _METRIC_RE.fullmatch(metric):
        raise ConfigError(f"{where}.metric: invalid metric name {metric!r}")

    labels = raw.get("labels", {})
    if not isinstance(labels, dict):
        raise ConfigError(f"{where}.labels: must be an object of string equality filters")
    for key, value in labels.items():
        if not isinstance(key, str) or not _LABEL_RE.fullmatch(key):
            raise ConfigError(f"{where}.labels: invalid label name {key!r}")
        if not isinstance(value, str):
            raise ConfigError(f"{where}.labels[{key!r}]: value must be a string")

    threshold = _number(raw.get("threshold"), f"{where}.threshold")

    duration = _number(raw.get("duration_seconds"), f"{where}.duration_seconds")
    if duration < 0:
        raise ConfigError(f"{where}.duration_seconds: must be >= 0")

    return RuleConfig(
        id=rid,
        target_id=target_id,
        metric=metric,
        labels=dict(labels),
        threshold=float(threshold),
        duration_seconds=float(duration),
    )


def _check_keys(raw: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)}")


def _check_unique(ids: list[str], what: str) -> None:
    seen: set[str] = set()
    for i in ids:
        if i in seen:
            raise ConfigError(f"duplicate {what} {i!r}")
        seen.add(i)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: must be a number")
    if not math.isfinite(value):
        raise ConfigError(f"{where}: must be finite")
    return float(value)
