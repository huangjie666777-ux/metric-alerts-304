"""Parser for a strict subset of the Prometheus text exposition format 0.0.4.

Supported:
  * ``# HELP`` / ``# TYPE`` comment directives (TYPE must be ``gauge``)
  * gauge samples with label sets and 0.0.4 label escaping (``\\``, ``\\"``, ``\\n``)

Rejected (each aborts the whole scrape round):
  * explicit timestamps on samples
  * non-finite sample values (NaN, +Inf, -Inf, overflow such as 1e999)
  * duplicate series (same metric + same label set, order-insensitive)
  * any other malformed input
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass


class ParseError(ValueError):
    """The exposition text violates the supported 0.0.4 subset."""


_METRIC_NAME_RE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL_NAME_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
_FINITE_FLOAT_RE = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?")
_NON_FINITE_TOKENS = {"NaN", "Inf", "+Inf", "-Inf"}

_TYPE_GAUGE = "gauge"


@dataclass(frozen=True)
class Sample:
    """One parsed series value. ``labels`` is sorted by label name."""

    metric: str
    labels: tuple[tuple[str, str], ...]
    value: float

    @property
    def labels_dict(self) -> dict[str, str]:
        return dict(self.labels)


def parse(text: str) -> list[Sample]:
    """Parse exposition text into samples, or raise ParseError."""
    types: dict[str, str] = {}
    helps: set[str] = set()
    sampled_metrics: set[str] = set()
    seen_series: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    samples: list[Sample] = []

    for lineno, raw in enumerate(text.split("\n"), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            _parse_comment(line, lineno, types, helps, sampled_metrics)
            continue
        sample = _parse_sample(line, lineno, types, sampled_metrics)
        key = (sample.metric, sample.labels)
        if key in seen_series:
            raise ParseError(
                f"line {lineno}: duplicate series for metric {sample.metric!r} "
                f"with labels {sample.labels_dict!r}"
            )
        seen_series.add(key)
        samples.append(sample)
    return samples


def _parse_comment(
    line: str,
    lineno: int,
    types: dict[str, str],
    helps: set[str],
    sampled_metrics: set[str],
) -> None:
    body = line[1:].lstrip()
    parts = body.split(None, 2)
    keyword = parts[0] if parts else ""
    if keyword not in ("HELP", "TYPE"):
        return  # plain comment
    if len(parts) < 2 or not parts[1]:
        raise ParseError(f"line {lineno}: malformed {keyword} directive")
    metric = parts[1]
    if not _METRIC_NAME_RE.fullmatch(metric):
        raise ParseError(f"line {lineno}: invalid metric name {metric!r} in {keyword}")
    if keyword == "HELP":
        if metric in helps:
            raise ParseError(f"line {lineno}: duplicate HELP for {metric!r}")
        helps.add(metric)
        return
    # TYPE directive
    mtype = parts[2].strip() if len(parts) > 2 else ""
    if mtype != _TYPE_GAUGE:
        raise ParseError(
            f"line {lineno}: unsupported TYPE {mtype!r} for {metric!r} (only 'gauge' is supported)"
        )
    if metric in types:
        raise ParseError(f"line {lineno}: duplicate TYPE for {metric!r}")
    if metric in sampled_metrics:
        raise ParseError(f"line {lineno}: TYPE for {metric!r} must precede its samples")
    types[metric] = mtype


def _parse_sample(
    line: str,
    lineno: int,
    types: dict[str, str],
    sampled_metrics: set[str],
) -> Sample:
    pos = 0
    m = _METRIC_NAME_RE.match(line, pos)
    if m is None:
        raise ParseError(f"line {lineno}: expected a metric name")
    metric = m.group(0)
    pos = m.end()

    labels: list[tuple[str, str]] = []
    if pos < len(line) and line[pos] == "{":
        pos += 1
        if pos < len(line) and line[pos] == "}":
            pos += 1  # empty label set is legal
        else:
            while True:
                m = _LABEL_NAME_RE.match(line, pos)
                if m is None:
                    raise ParseError(f"line {lineno}: expected a label name")
                name = m.group(0)
                pos = m.end()
                if pos >= len(line) or line[pos] != "=":
                    raise ParseError(f"line {lineno}: expected '=' after label {name!r}")
                pos += 1
                if pos >= len(line) or line[pos] != '"':
                    raise ParseError(f"line {lineno}: expected '\"' to open label {name!r} value")
                value, pos = _parse_label_value(line, pos + 1, lineno)
                labels.append((name, value))
                if pos >= len(line):
                    raise ParseError(f"line {lineno}: unterminated label set")
                ch = line[pos]
                if ch == ",":
                    pos += 1
                    continue
                if ch == "}":
                    pos += 1
                    break
                raise ParseError(f"line {lineno}: expected ',' or '}}' in label set")

    names = [n for n, _ in labels]
    if len(set(names)) != len(names):
        raise ParseError(f"line {lineno}: duplicate label name in series {metric!r}")

    if pos >= len(line) or line[pos] not in " \t":
        raise ParseError(f"line {lineno}: expected whitespace before the sample value")
    while pos < len(line) and line[pos] in " \t":
        pos += 1
    start = pos
    while pos < len(line) and line[pos] not in " \t":
        pos += 1
    token = line[start:pos]
    value = _parse_value(token, lineno)

    rest = line[pos:].strip()
    if rest:
        raise ParseError(
            f"line {lineno}: explicit timestamps are not supported (got {rest!r})"
        )

    # A metric without a TYPE directive defaults to gauge in our subset; any
    # other TYPE was already rejected while parsing directives.
    sampled_metrics.add(metric)
    labels.sort()
    return Sample(metric=metric, labels=tuple(labels), value=value)


def _parse_label_value(line: str, pos: int, lineno: int) -> tuple[str, int]:
    out: list[str] = []
    while True:
        if pos >= len(line):
            raise ParseError(f"line {lineno}: unterminated label value")
        ch = line[pos]
        if ch == '"':
            return "".join(out), pos + 1
        if ch == "\\":
            pos += 1
            if pos >= len(line):
                raise ParseError(f"line {lineno}: unterminated escape sequence")
            esc = line[pos]
            if esc == "n":
                out.append("\n")
            elif esc == "\\":
                out.append("\\")
            elif esc == '"':
                out.append('"')
            else:
                raise ParseError(f"line {lineno}: invalid escape sequence '\\{esc}'")
            pos += 1
            continue
        out.append(ch)
        pos += 1


def _parse_value(token: str, lineno: int) -> float:
    if token in _NON_FINITE_TOKENS:
        raise ParseError(f"line {lineno}: non-finite sample value {token!r}")
    if not _FINITE_FLOAT_RE.fullmatch(token):
        raise ParseError(f"line {lineno}: invalid sample value {token!r}")
    value = float(token)
    if not math.isfinite(value):  # e.g. 1e999 overflows to inf
        raise ParseError(f"line {lineno}: non-finite sample value {token!r}")
    return value
