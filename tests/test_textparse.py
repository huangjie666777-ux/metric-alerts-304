import pytest

from app.textparse import ParseError, parse


def test_simple_gauge_with_type():
    samples = parse("# HELP temp Temperature\n# TYPE temp gauge\ntemp 21.5\n")
    assert len(samples) == 1
    s = samples[0]
    assert s.metric == "temp"
    assert s.labels == ()
    assert s.value == 21.5


def test_metric_without_type_defaults_to_gauge():
    samples = parse("up 1\n")
    assert samples[0].value == 1.0


def test_labels_are_sorted_and_order_insensitive():
    a = parse('m{b="2",a="1"} 5\n')[0]
    b = parse('m{a="1",b="2"} 5\n')[0]
    assert a.labels == b.labels == (("a", "1"), ("b", "2"))


def test_empty_label_set_is_legal():
    samples = parse("m{} 3\n")
    assert samples[0].labels == ()


def test_label_escapes():
    samples = parse('m{a="line\\nbreak",b="quo\\"te",c="back\\\\slash"} 1\n')
    d = samples[0].labels_dict
    assert d["a"] == "line\nbreak"
    assert d["b"] == 'quo"te'
    assert d["c"] == "back\\slash"


@pytest.mark.parametrize("text", [
    'm{a="tab\\tchar"} 1\n',   # invalid escape
    'm{a="unterminated} 1\n',  # unterminated value
    'm{a="x" 1\n',             # missing closing brace
    'm{a="x",} 1\n',           # trailing comma
    'm{a="1",a="2"} 1\n',      # duplicate label name
    'm{a=x} 1\n',              # unquoted value
    'm{1a="x"} 1\n',           # bad label name
])
def test_label_syntax_errors(text):
    with pytest.raises(ParseError):
        parse(text)


def test_duplicate_series_rejected_regardless_of_label_order():
    with pytest.raises(ParseError, match="duplicate series"):
        parse('m{a="1",b="2"} 1\nm{b="2",a="1"} 2\n')


def test_same_metric_different_labels_ok():
    samples = parse('m{a="1"} 1\nm{a="2"} 2\n')
    assert len(samples) == 2


def test_explicit_timestamp_rejected():
    with pytest.raises(ParseError, match="timestamp"):
        parse("m 1 1710000000000\n")


@pytest.mark.parametrize("token", ["NaN", "+Inf", "-Inf", "Inf", "1e999", "-1e999"])
def test_non_finite_values_rejected(token):
    with pytest.raises(ParseError, match="non-finite"):
        parse(f"m {token}\n")


@pytest.mark.parametrize("token", ["1", "-2.5", ".5", "5.", "1e3", "-2.5E-2", "+4"])
def test_finite_value_formats(token):
    samples = parse(f"m {token}\n")
    assert samples[0].value == float(token)


@pytest.mark.parametrize("token", ["abc", "1_000", "0x10", "1.2.3", ""])
def test_invalid_value_tokens(token):
    with pytest.raises(ParseError):
        parse(f"m {token}\n" if token else "m \n")


@pytest.mark.parametrize("mtype", ["counter", "histogram", "summary", "untyped", "gauge extra"])
def test_non_gauge_types_rejected(mtype):
    with pytest.raises(ParseError, match="unsupported TYPE"):
        parse(f"# TYPE m {mtype}\nm 1\n")


def test_type_after_samples_rejected():
    with pytest.raises(ParseError, match="must precede"):
        parse("m 1\n# TYPE m gauge\n")


def test_duplicate_type_rejected():
    with pytest.raises(ParseError, match="duplicate TYPE"):
        parse("# TYPE m gauge\n# TYPE m gauge\nm 1\n")


def test_duplicate_help_rejected():
    with pytest.raises(ParseError, match="duplicate HELP"):
        parse("# HELP m a\n# HELP m b\nm 1\n")


def test_plain_comments_and_blank_lines_ignored():
    text = "# just a comment\n\n#TYPEX not a directive\nm 1\n"
    assert len(parse(text)) == 1


@pytest.mark.parametrize("text", [
    "1m 1\n",            # bad metric name
    "m\n",               # missing value
    "m 1 garbage\n",     # trailing tokens
    "# TYPE 1m gauge\n", # bad metric in directive
])
def test_malformed_lines(text):
    with pytest.raises(ParseError):
        parse(text)


def test_empty_exposition_is_valid():
    assert parse("") == []
    assert parse("# nothing here\n") == []
