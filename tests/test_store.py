import pytest

from app.alerts import (
    EVENT_FIRING,
    EVENT_RESOLVED,
    REASON_RESTART,
    AlertEngine,
    canonical_labels,
)
from app.config import RuleConfig
from app.store import Store
from app.textparse import Sample


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


def make_rule(**kw):
    defaults = dict(id="r1", target_id="t1", metric="m", labels={},
                    threshold=10.0, duration_seconds=0.0)
    defaults.update(kw)
    return RuleConfig(**defaults)


def sample(value, metric="m", labels=()):
    return Sample(metric=metric, labels=tuple(sorted(labels)), value=float(value))


def round_trip(store, engine, target, samples, mono, wall):
    trs = engine.evaluate_success(target, samples, mono, wall)
    store.persist_round(target_id=target, ok=True, error=None,
                        samples=samples, transitions=trs, ts_wall=wall)
    engine.apply(trs)
    return trs


def test_round_and_events_persisted_atomically(store):
    eng = AlertEngine([make_rule()])
    round_trip(store, eng, "t1", [sample(20)], mono=0.0, wall=100.0)

    health = store.target_health("t1")
    assert health["last_ok"] is True
    assert health["last_sample_count"] == 1
    assert health["rounds_total"] == 1

    alerts = store.list_active_alerts()
    assert len(alerts) == 1
    assert alerts[0]["rule_id"] == "r1"
    assert alerts[0]["value"] == 20.0

    events = store.list_events(0, 10)
    assert len(events) == 1
    assert events[0]["event"] == EVENT_FIRING
    assert events[0]["reason"] is None


def test_failed_round_keeps_previous_samples(store):
    eng = AlertEngine([make_rule()])
    round_trip(store, eng, "t1", [sample(20)], mono=0.0, wall=100.0)

    trs = eng.evaluate_failure("t1", 101.0)
    store.persist_round(target_id="t1", ok=False, error="boom",
                        samples=[], transitions=trs, ts_wall=101.0)
    eng.apply(trs)

    health = store.target_health("t1")
    assert health["last_ok"] is False
    assert health["last_error"] == "boom"
    assert health["last_success_ts"] == 100.0
    assert health["rounds_failed"] == 1
    # previous sample snapshot still there, no partial data
    rows = store.list_samples("t1")
    assert len(rows) == 1 and rows[0]["value"] == 20.0
    # firing alert resolved by the failure
    assert store.list_active_alerts() == []
    events = store.list_events(0, 10)
    assert [e["event"] for e in events] == [EVENT_FIRING, EVENT_RESOLVED]
    assert events[1]["reason"] == "scrape_failed"


def test_restart_resolves_firing_once(store):
    eng = AlertEngine([make_rule()])
    round_trip(store, eng, "t1", [sample(20)], mono=0.0, wall=100.0)

    resolved = store.resolve_firing_on_restart(REASON_RESTART, 200.0)
    assert len(resolved) == 1
    assert store.list_active_alerts() == []
    # a second restart does nothing
    assert store.resolve_firing_on_restart(REASON_RESTART, 300.0) == []
    events = store.list_events(0, 10)
    assert [e["event"] for e in events] == [EVENT_FIRING, EVENT_RESOLVED]
    assert events[1]["reason"] == REASON_RESTART


def test_event_ids_stable_and_paginated(store):
    eng = AlertEngine([make_rule()])
    round_trip(store, eng, "t1", [sample(20)], mono=0.0, wall=1.0)   # firing id=1
    round_trip(store, eng, "t1", [sample(5)], mono=1.0, wall=2.0)    # resolved id=2
    round_trip(store, eng, "t1", [sample(30)], mono=2.0, wall=3.0)   # firing id=3

    page1 = store.list_events(0, 2)
    assert [e["id"] for e in page1] == [1, 2]
    page2 = store.list_events(page1[-1]["id"], 2)
    assert [e["id"] for e in page2] == [3]
    assert store.list_events(3, 2) == []


def test_history_survives_reopen(store, tmp_path):
    eng = AlertEngine([make_rule()])
    round_trip(store, eng, "t1", [sample(20)], mono=0.0, wall=1.0)
    store.close()

    store2 = Store(str(tmp_path / "test.db"))
    try:
        assert len(store2.list_events(0, 10)) == 1
        assert len(store2.list_active_alerts()) == 1
    finally:
        store2.close()


def test_labels_key_canonical(store):
    eng = AlertEngine([make_rule()])
    s = Sample(metric="m", labels=(("b", "2"), ("a", "1")), value=1.0)
    round_trip(store, eng, "t1", [s], mono=0.0, wall=1.0)
    row = store.list_samples("t1")[0]
    assert row["labels_json"] == canonical_labels({"a": "1", "b": "2"})
