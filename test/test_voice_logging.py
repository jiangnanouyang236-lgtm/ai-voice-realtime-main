import io
import json
import logging

from voice_logging import configure_logging


def test_configure_logging_json_redacts_sensitive_values(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setenv("VOICE_LOG_FORMAT", "json")
    monkeypatch.setenv("VOICE_LOG_STREAM", "stdout")
    monkeypatch.setattr("sys.stdout", stream)

    configure_logging("unit-test", force=True)
    logging.getLogger("voice.test").info(
        "robot_secret=plain-text-secret",
        extra={"robot_secret": "another-secret", "round_id": "r1"},
    )

    payload = json.loads(stream.getvalue())
    assert payload["service"] == "unit-test"
    assert payload["logger"] == "voice.test"
    assert payload["message"] == "robot_secret=[redacted]"
    assert payload["robot_secret"] == "[redacted]"
    assert payload["round_id"] == "r1"


def test_configure_logging_text_includes_service(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setenv("VOICE_LOG_FORMAT", "text")
    monkeypatch.setenv("VOICE_LOG_COLOR", "never")
    monkeypatch.setattr("sys.stdout", stream)

    configure_logging("gateway", force=True)
    logging.getLogger("voice.gateway").warning("hello")

    output = stream.getvalue()
    assert "service=gateway" in output
    assert "logger=voice.gateway" in output
    assert "WARNING" in output
