"""Sustained-threshold alert state machine.

Alert identity is (rule, full label set); a rule belongs to exactly one
target, so the target is part of the identity implicitly. Label order never
affects identity because labels are canonicalized (sorted) before keying.

Semantics:
  * value > threshold          -> pending (timer starts on the monotonic clock)
  * pending for duration       -> firing (one "firing" event)
  * duration == 0              -> fires immediately
  * value back under threshold -> pending cleared silently / firing resolved
  * series disappears          -> pending cleared / firing resolved
  * scrape round fails         -> pending cleared / firing resolved
  * a failed round never contributes to a pending timer (no accumulation)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .config import RuleConfig
from .textparse import Sample

PHASE_PENDING = "pending"
PHASE_FIRING = "firing"

EVENT_FIRING = "firing"
EVENT_RESOLVED = "resolved"

REASON_RECOVERED = "recovered"            # value dropped back under the threshold
REASON_SERIES_MISSING = "series_missing"  # series no longer exposed
REASON_SCRAPE_FAILED = "scrape_failed"    # the scrape round failed
REASON_RESTART = "restart"                # process restarted while firing


def canonical_labels(labels: dict[str, str]) -> str:
    """Deterministic JSON encoding; label order never changes the result."""
    return json.dumps(labels, sort_keys=True, separators=(",", ":"))


@dataclass
class SeriesState:
    phase: str                 # PHASE_PENDING | PHASE_FIRING
    since_mono: float          # monotonic time when the violation started
    fired_wall: float | None   # wall time when it started firing
    value: float               # last observed value


@dataclass(frozen=True)
class AlertEvent:
    rule_id: str
    target_id: str
    labels_json: str           # canonical label JSON (also the identity key)
    event: str                 # EVENT_FIRING | EVENT_RESOLVED
    reason: str | None         # only for resolved events
    value: float
    ts_wall: float


@dataclass
class RuleTransition:
    """State delta for one rule produced by evaluating one round."""

    rule_id: str
    target_id: str
    events: list[AlertEvent] = field(default_factory=list)
    # labels_key -> (labels_json, value, fired_wall) rows to upsert
    upserts: dict[str, tuple[str, float, float]] = field(default_factory=dict)
    deletes: list[str] = field(default_factory=list)  # labels_keys no longer active
    new_states: dict[str, SeriesState] = field(default_factory=dict)


class AlertEngine:
    """Evaluates scrape rounds against rules and produces transitions.

    The engine is deliberately split into pure evaluation (``evaluate_*``)
    and mutation (``apply``) so callers can persist a transition first and
    only then commit it to memory, keeping RAM and SQLite consistent.
    """

    def __init__(self, rules: tuple[RuleConfig, ...] | list[RuleConfig]):
        self._rules = list(rules)
        self._by_target: dict[str, list[RuleConfig]] = {}
        for rule in self._rules:
            self._by_target.setdefault(rule.target_id, []).append(rule)
        self._states: dict[str, dict[str, SeriesState]] = {
            rule.id: {} for rule in self._rules
        }

    def evaluate_success(
        self,
        target_id: str,
        samples: list[Sample],
        now_mono: float,
        now_wall: float,
    ) -> list[RuleTransition]:
        return [
            self._evaluate_rule_success(rule, samples, now_mono, now_wall)
            for rule in self._by_target.get(target_id, [])
        ]

    def evaluate_failure(self, target_id: str, now_wall: float) -> list[RuleTransition]:
        transitions: list[RuleTransition] = []
        for rule in self._by_target.get(target_id, []):
            tr = RuleTransition(rule_id=rule.id, target_id=rule.target_id)
            for key, st in self._states[rule.id].items():
                if st.phase == PHASE_FIRING:
                    tr.events.append(
                        AlertEvent(
                            rule_id=rule.id,
                            target_id=rule.target_id,
                            labels_json=key,
                            event=EVENT_RESOLVED,
                            reason=REASON_SCRAPE_FAILED,
                            value=st.value,
                            ts_wall=now_wall,
                        )
                    )
                    tr.deletes.append(key)
            tr.new_states = {}  # pending timers never survive a failed round
            transitions.append(tr)
        return transitions

    def apply(self, transitions: list[RuleTransition]) -> None:
        for tr in transitions:
            self._states[tr.rule_id] = tr.new_states

    def pending_count(self) -> int:
        return sum(
            1
            for states in self._states.values()
            for st in states.values()
            if st.phase == PHASE_PENDING
        )

    # ------------------------------------------------------------------

    def _evaluate_rule_success(
        self,
        rule: RuleConfig,
        samples: list[Sample],
        now_mono: float,
        now_wall: float,
    ) -> RuleTransition:
        states = self._states[rule.id]
        tr = RuleTransition(rule_id=rule.id, target_id=rule.target_id)

        # Series matching this rule, keyed by their canonical full label set.
        matches: dict[str, Sample] = {}
        for sample in samples:
            if sample.metric != rule.metric:
                continue
            labels = sample.labels_dict
            if all(labels.get(k) == v for k, v in rule.labels.items()):
                matches[canonical_labels(labels)] = sample

        for key, sample in matches.items():
            st = states.get(key)
            value = sample.value
            if value > rule.threshold:
                if st is None:
                    if rule.duration_seconds <= 0:
                        tr.new_states[key] = SeriesState(
                            PHASE_FIRING, now_mono, now_wall, value
                        )
                        tr.events.append(self._event(rule, key, EVENT_FIRING, None, value, now_wall))
                        tr.upserts[key] = (key, value, now_wall)
                    else:
                        tr.new_states[key] = SeriesState(
                            PHASE_PENDING, now_mono, None, value
                        )
                elif st.phase == PHASE_PENDING:
                    if now_mono - st.since_mono >= rule.duration_seconds:
                        tr.new_states[key] = SeriesState(
                            PHASE_FIRING, st.since_mono, now_wall, value
                        )
                        tr.events.append(self._event(rule, key, EVENT_FIRING, None, value, now_wall))
                        tr.upserts[key] = (key, value, now_wall)
                    else:
                        tr.new_states[key] = SeriesState(
                            PHASE_PENDING, st.since_mono, None, value
                        )
                else:  # already firing, still violating: no duplicate event
                    tr.new_states[key] = SeriesState(
                        PHASE_FIRING, st.since_mono, st.fired_wall, value
                    )
                    tr.upserts[key] = (key, value, st.fired_wall)
            else:
                if st is not None and st.phase == PHASE_FIRING:
                    tr.events.append(self._event(rule, key, EVENT_RESOLVED, REASON_RECOVERED, value, now_wall))
                    tr.deletes.append(key)
                # pending (or unknown) series that recovered: cleared silently

        # Series that vanished from the exposition.
        for key, st in states.items():
            if key not in matches and st.phase == PHASE_FIRING:
                tr.events.append(self._event(rule, key, EVENT_RESOLVED, REASON_SERIES_MISSING, st.value, now_wall))
                tr.deletes.append(key)

        return tr

    @staticmethod
    def _event(
        rule: RuleConfig,
        labels_json: str,
        event: str,
        reason: str | None,
        value: float,
        ts_wall: float,
    ) -> AlertEvent:
        return AlertEvent(
            rule_id=rule.id,
            target_id=rule.target_id,
            labels_json=labels_json,
            event=event,
            reason=reason,
            value=value,
            ts_wall=ts_wall,
        )
