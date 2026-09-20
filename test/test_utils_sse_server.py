from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_servers import utils_api
from mcp_servers import utils_sse_server


def test_get_lunar_date_accepts_timezone_aware_now():
    result = utils_api.get_lunar_date("UTC")

    assert result
    assert not result.startswith("查询失败")


def test_tools_call_unexpected_error_returns_jsonrpc_error(monkeypatch):
    monkeypatch.setattr(
        utils_sse_server,
        "_call_tool",
        lambda name, arguments: (_ for _ in ()).throw(
            TypeError("can't subtract offset-naive and offset-aware datetimes")
        ),
    )

    response = utils_sse_server.handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_now_context", "arguments": {"timezone": "UTC"}},
        }
    )

    assert response == {
        "jsonrpc": "2.0",
        "id": 2,
        "error": {
            "code": -32603,
            "message": "Tool execution failed: can't subtract offset-naive and offset-aware datetimes",
        },
    }


def test_messages_endpoint_returns_202_when_tool_raises(monkeypatch):
    session_id = "test-session"
    msg_queue = utils_sse_server.queue.Queue()

    monkeypatch.setitem(utils_sse_server.sessions, session_id, msg_queue)
    monkeypatch.setattr(
        utils_sse_server,
        "_call_tool",
        lambda name, arguments: (_ for _ in ()).throw(
            TypeError("can't subtract offset-naive and offset-aware datetimes")
        ),
    )

    try:
        with utils_sse_server.app.test_client() as client:
            resp = client.post(
                f"/messages/{session_id}",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "get_now_context", "arguments": {"timezone": "UTC"}},
                },
            )

        assert resp.status_code == 202
        queued = msg_queue.get_nowait()
        assert queued["error"]["code"] == -32603
    finally:
        utils_sse_server.sessions.pop(session_id, None)
