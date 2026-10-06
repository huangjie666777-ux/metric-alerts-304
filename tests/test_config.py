import pytest

from app.config import ConfigError, load_config, parse_config

VALID = {
    "port": 8080,
    "sqlite_path": "alerts.db",
    "targets": [
        {
            "id": "t1",
            "url": "http://127.0.0.1:9100/metrics",
            "interval_seconds": 2,
            "timeout_seconds": 1.5,
            "max_response_bytes": 65536,
        }
    ],
    "rules": [
        {
            "id": "r1",
            "target_id": "t1",
            "metric": "m",
            "labels": {"a": "1"},
            "threshold": 10,
            "duration_seconds": 5,
        }
    ],
}


def test_valid_config():
    cfg = parse_config(VALID)
    assert cfg.port == 8080
    assert cfg.host == "127.0.0.1"
    assert cfg.targets[0].interval_seconds == 2.0
    assert cfg.rules[0].labels == {"a": "1"}


def test_labels_optional():
    import copy
    raw = copy.deepcopy(VALID)
    del raw["rules"][0]["labels"]
    assert parse_config(raw).rules[0].labels == {}


@pytest.mark.parametrize("mutate, match", [
    (lambda r: r.update(port=0), "port"),
    (lambda r: r.update(port=True), "port"),
    (lambda r: r.update(port=70000), "port"),
    (lambda r: r.update(sqlite_path=""), "sqlite_path"),
    (lambda r: r.update(unknown=1), "unknown key"),
    (lambda r: r["targets"].append(r["targets"][0]), "duplicate target id"),
    (lambda r: r["rules"].append(r["rules"][0]), "duplicate rule id"),
    (lambda r: r["rules"][0].update(target_id="nope"), "unknown target_id"),
    (lambda r: r["targets"][0].update(url="ftp://x"), "url"),
    (lambda r: r["targets"][0].update(url="http://"), "url"),
    (lambda r: r["targets"][0].update(interval_seconds=0), "interval"),
    (lambda r: r["targets"][0].update(interval_seconds=-1), "interval"),
    (lambda r: r["targets"][0].update(timeout_seconds=-0.5), "timeout"),
    (lambda r: r["targets"][0].update(max_response_bytes=0), "max_response_bytes"),
    (lambda r: r["targets"][0].update(max_response_bytes=1.5), "max_response_bytes"),
    (lambda r: r["rules"][0].update(metric="1bad"), "metric"),
    (lambda r: r["rules"][0].update(labels={"1bad": "x"}), "label name"),
    (lambda r: r["rules"][0].update(labels={"ok": 5}), "value must be a string"),
    (lambda r: r["rules"][0].update(threshold="high"), "threshold"),
    (lambda r: r["rules"][0].update(duration_seconds=-1), "duration"),
    (lambda r: r["rules"][0].update(duration_seconds=True), "duration"),
])
def test_invalid_configs(mutate, match):
    import copy
    raw = copy.deepcopy(VALID)
    mutate(raw)
    with pytest.raises(ConfigError, match=match):
        parse_config(raw)


def test_nan_literal_rejected(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"port": 8080, "sqlite_path": "x", "targets": [], "rules": [], "x": NaN}')
    with pytest.raises(ConfigError):
        load_config(p)


def test_missing_file():
    with pytest.raises(ConfigError, match="cannot read"):
        load_config("/nonexistent/config.json")


def test_zero_duration_allowed():
    import copy
    raw = copy.deepcopy(VALID)
    raw["rules"][0]["duration_seconds"] = 0
    assert parse_config(raw).rules[0].duration_seconds == 0.0
