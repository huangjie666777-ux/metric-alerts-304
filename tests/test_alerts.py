from app.alerts import (
    EVENT_FIRING,
    EVENT_RESOLVED,
    REASON_RECOVERED,
    REASON_SCRAPE_FAILED,
    REASON_SERIES_MISSING,
    AlertEngine,
    canonical_labels,
)
from app.config import RuleConfig
from app.textparse import Sample


def make_rule(**kw):
    defaults = dict(
        id="r1", target_id="t1", metric="m", labels={},
        threshold=10.0, duration_seconds=5.0,
    )
    defaults.update(kw)
    return RuleConfig(**defaults)


def sample(value, metric="m", labels=()):
    return Sample(metric=metric, labels=tuple(sorted(labels)), value=float(value))


def success(engine, target, samples, mono, wall=1000.0):
    trs = engine.evaluate_success(target, samples, mono, wall)
    engine.apply(trs)
    return trs


def failure(engine, target, wall=1000.0):
    trs = engine.evaluate_failure(target, wall)
    engine.apply(trs)
    return trs


def events_of(trs):
    return [e for tr in trs for e in tr.events]


def test_zero_duration_fires_immediately():
    eng = AlertEngine([make_rule(duration_seconds=0)])
    trs = success(eng, "t1", [sample(20)], mono=0.0)
    evs = events_of(trs)
    assert len(evs) == 1 and evs[0].event == EVENT_FIRING
    assert trs[0].upserts  # active alert persisted


def test_pending_then_firing_after_duration():
    eng = AlertEngine([make_rule(duration_seconds=5)])
    assert events_of(success(eng, "t1", [sample(20)], mono=0.0)) == []
    assert events_of(success(eng, "t1", [sample(20)], mono=4.9)) == []
    evs = events_of(success(eng, "t1", [sample(20)], mono=5.0))
    assert len(evs) == 1 and evs[0].event == EVENT_FIRING


def test_persistent_violation_fires_only_once():
    eng = AlertEngine([make_rule(duration_seconds=0)])
    assert len(events_of(success(eng, "t1", [sample(20)], mono=0.0))) == 1
    for i in range(1, 5):
        assert events_of(success(eng, "t1", [sample(20 + i)], mono=float(i))) == []


def test_recovery_before_duration_is_silent_and_resets_timer():
    eng = AlertEngine([make_rule(duration_seconds=5)])
    success(eng, "t1", [sample(20)], mono=0.0)
    # recovers at t=2 (silent), violates again at t=3 -> timer restarts
    assert events_of(success(eng, "t1", [sample(5)], mono=2.0)) == []
    assert events_of(success(eng, "t1", [sample(20)], mono=3.0)) == []
    assert events_of(success(eng, "t1", [sample(20)], mono=7.9)) == []
    evs = events_of(success(eng, "t1", [sample(20)], mono=8.0))
    assert len(evs) == 1 and evs[0].event == EVENT_FIRING


def test_firing_then_recovery_resolves_with_reason():
    eng = AlertEngine([make_rule(duration_seconds=0)])
    success(eng, "t1", [sample(20)], mono=0.0)
    evs = events_of(success(eng, "t1", [sample(5)], mono=1.0))
    assert len(evs) == 1
    assert evs[0].event == EVENT_RESOLVED
    assert evs[0].reason == REASON_RECOVERED
    assert evs[0].value == 5.0
    # re-exceeding starts a fresh cycle
    evs = events_of(success(eng, "t1", [sample(30)], mono=2.0))
    assert len(evs) == 1 and evs[0].event == EVENT_FIRING


def test_missing_series_resolves_firing_and_clears_pending():
    eng = AlertEngine([make_rule(duration_seconds=0)])
    success(eng, "t1", [sample(20)], mono=0.0)
    evs = events_of(success(eng, "t1", [], mono=1.0))
    assert len(evs) == 1
    assert evs[0].reason == REASON_SERIES_MISSING

    eng2 = AlertEngine([make_rule(duration_seconds=5)])
    success(eng2, "t1", [sample(20)], mono=0.0)  # pending
    assert events_of(success(eng2, "t1", [], mono=1.0)) == []  # gone, silent
    # comes back violating: timer starts from scratch
    assert events_of(success(eng2, "t1", [sample(20)], mono=2.0)) == []
    assert events_of(success(eng2, "t1", [sample(20)], mono=6.9)) == []
    assert len(events_of(success(eng2, "t1", [sample(20)], mono=7.0))) == 1


def test_failed_round_resolves_firing_and_clears_pending():
    eng = AlertEngine([make_rule(duration_seconds=0)])
    success(eng, "t1", [sample(20)], mono=0.0)
    evs = events_of(failure(eng, "t1"))
    assert len(evs) == 1 and evs[0].reason == REASON_SCRAPE_FAILED
    assert events_of(failure(eng, "t1")) == []  # no duplicate resolution

    eng2 = AlertEngine([make_rule(duration_seconds=5)])
    success(eng2, "t1", [sample(20)], mono=0.0)   # pending at t=0
    failure(eng2, "t1")                            # t=1 failure clears pending
    success(eng2, "t1", [sample(20)], mono=2.0)   # timer restarts at t=2
    assert events_of(success(eng2, "t1", [sample(20)], mono=6.9)) == []
    evs = events_of(success(eng2, "t1", [sample(20)], mono=7.0))
    assert len(evs) == 1 and evs[0].event == EVENT_FIRING


def test_label_order_does_not_change_identity():
    eng = AlertEngine([make_rule(duration_seconds=5)])
    success(eng, "t1", [sample(20, labels=(("b", "2"), ("a", "1")))], mono=0.0)
    # same series, labels in different order -> same pending timer
    evs = events_of(success(eng, "t1", [sample(20, labels=(("a", "1"), ("b", "2")))], mono=5.0))
    assert len(evs) == 1 and evs[0].event == EVENT_FIRING
    assert evs[0].labels_json == canonical_labels({"a": "1", "b": "2"})


def test_different_label_sets_are_independent_alerts():
    eng = AlertEngine([make_rule(duration_seconds=0)])
    trs = success(eng, "t1", [sample(20, labels=(("i", "a"),)),
                              sample(20, labels=(("i", "b"),))], mono=0.0)
    assert len(events_of(trs)) == 2
    # one recovers, the other keeps firing
    evs = events_of(success(eng, "t1", [sample(5, labels=(("i", "a"),)),
                                        sample(20, labels=(("i", "b"),))], mono=1.0))
    assert len(evs) == 1 and evs[0].reason == REASON_RECOVERED
    assert evs[0].labels_json == canonical_labels({"i": "a"})


def test_rule_label_filter_matches_subset():
    rule = make_rule(labels={"job": "web"}, duration_seconds=0)
    eng = AlertEngine([rule])
    trs = success(eng, "t1", [
        sample(20, labels=(("job", "web"), ("inst", "1"))),   # matches
        sample(20, labels=(("job", "db"),)),                  # filtered out
        sample(20, metric="other", labels=(("job", "web"),)),  # wrong metric
    ], mono=0.0)
    evs = events_of(trs)
    assert len(evs) == 1
    assert evs[0].labels_json == canonical_labels({"job": "web", "inst": "1"})


def test_threshold_is_strictly_greater():
    eng = AlertEngine([make_rule(threshold=10.0, duration_seconds=0)])
    assert events_of(success(eng, "t1", [sample(10)], mono=0.0)) == []  # equal: no
    assert len(events_of(success(eng, "t1", [sample(10.0001)], mono=1.0))) == 1


def test_rules_are_scoped_to_their_target():
    rule = make_rule(target_id="t1", duration_seconds=0)
    eng = AlertEngine([rule])
    assert success(eng, "t2", [sample(20)], mono=0.0) == []
    assert failure(eng, "t2") == []
