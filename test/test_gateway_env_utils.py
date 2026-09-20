from gateway.env_utils import get_env_bool, get_env_float, get_env_int


def test_get_env_int_uses_default_for_missing_or_invalid_value(monkeypatch):
    monkeypatch.delenv("GATEWAY_TEST_INT", raising=False)
    assert get_env_int("GATEWAY_TEST_INT", 7) == 7

    monkeypatch.setenv("GATEWAY_TEST_INT", "bad")
    assert get_env_int("GATEWAY_TEST_INT", 7) == 7


def test_get_env_int_parses_valid_value(monkeypatch):
    monkeypatch.setenv("GATEWAY_TEST_INT", "42")

    assert get_env_int("GATEWAY_TEST_INT", 7) == 42


def test_get_env_float_uses_default_for_missing_or_invalid_value(monkeypatch):
    monkeypatch.delenv("GATEWAY_TEST_FLOAT", raising=False)
    assert get_env_float("GATEWAY_TEST_FLOAT", 1.5) == 1.5

    monkeypatch.setenv("GATEWAY_TEST_FLOAT", "bad")
    assert get_env_float("GATEWAY_TEST_FLOAT", 1.5) == 1.5


def test_get_env_float_parses_valid_value(monkeypatch):
    monkeypatch.setenv("GATEWAY_TEST_FLOAT", "2.25")

    assert get_env_float("GATEWAY_TEST_FLOAT", 1.5) == 2.25


def test_get_env_bool_uses_default_only_when_missing(monkeypatch):
    monkeypatch.delenv("GATEWAY_TEST_BOOL", raising=False)
    assert get_env_bool("GATEWAY_TEST_BOOL", True) is True
    assert get_env_bool("GATEWAY_TEST_BOOL", False) is False


def test_get_env_bool_accepts_common_truthy_values(monkeypatch):
    for value in ("1", "true", "yes", "on", " TRUE "):
        monkeypatch.setenv("GATEWAY_TEST_BOOL", value)
        assert get_env_bool("GATEWAY_TEST_BOOL", False) is True


def test_get_env_bool_treats_other_values_as_false(monkeypatch):
    for value in ("0", "false", "no", "off", "bad"):
        monkeypatch.setenv("GATEWAY_TEST_BOOL", value)
        assert get_env_bool("GATEWAY_TEST_BOOL", True) is False
