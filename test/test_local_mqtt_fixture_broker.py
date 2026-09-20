import json
import threading
from pathlib import Path

from paho.mqtt import publish
import pytest

from scripts.local_mqtt_fixture_broker import MQTTFixtureBroker


def test_fixture_broker_records_real_mqtt_publish(tmp_path: Path) -> None:
    record_path = tmp_path / "mqtt.jsonl"
    try:
        broker = MQTTFixtureBroker(("127.0.0.1", 0), record_path)
    except PermissionError:
        pytest.skip("sandbox does not permit localhost socket binding")
    thread = threading.Thread(target=broker.serve_forever, daemon=True)
    thread.start()
    try:
        publish.single(
            "windaka/fixture-robot/mcp/manual_control",
            payload=json.dumps(
                {
                    "msg_id": 1,
                    "meta": {"session_id": "session-a", "trace_id": "trace-a"},
                }
            ),
            hostname="127.0.0.1",
            port=broker.server_address[1],
        )
    finally:
        broker.shutdown()
        broker.server_close()
        thread.join(timeout=2)

    records = [json.loads(line) for line in record_path.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["topic"] == "windaka/fixture-robot/mcp/manual_control"
    assert records[0]["payload"]["meta"] == {
        "session_id": "session-a",
        "trace_id": "trace-a",
    }
