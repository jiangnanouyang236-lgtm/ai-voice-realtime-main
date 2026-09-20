from gateway.llm_metrics import parse_llm_metrics_json


def test_parse_llm_metrics_json_returns_empty_for_missing_invalid_or_non_object():
    assert parse_llm_metrics_json(None) == {}
    assert parse_llm_metrics_json("") == {}
    assert parse_llm_metrics_json("{bad-json") == {}
    assert parse_llm_metrics_json("[1, 2]") == {}


def test_parse_llm_metrics_json_returns_object_metrics():
    assert parse_llm_metrics_json('{"llm_first_text_ms": 123, "router": "chat"}') == {
        "llm_first_text_ms": 123,
        "router": "chat",
    }
