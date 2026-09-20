import asyncio

import pytest

from gateway.robot_session import (
    build_registered_message_payload,
    register_robot_session,
    resolve_request_bot,
)


def test_register_robot_session_requires_robot_id():
    with pytest.raises(ValueError, match="缺少 robot_id"):
        asyncio.run(
            register_robot_session(
                session_manager=_FakeSessionManager(),
                runtime_state=_FakeRuntimeState(),
                session_id="session-1",
                payload={},
                robot_secret_required=False,
            )
        )


def test_register_robot_session_registers_session_and_returns_runtime_row():
    session_manager = _FakeSessionManager()
    runtime_state = _FakeRuntimeState()

    registration = asyncio.run(
        register_robot_session(
            session_manager=session_manager,
            runtime_state=runtime_state,
            session_id="session-1",
            payload={
                "robot_id": " test_01 ",
                "client_type": " rust ",
                "robot_secret": "secret-ok",
            },
            robot_secret_required=True,
        )
    )

    assert runtime_state.register_calls == [("test_01", "rust", False)]
    assert runtime_state.verify_calls == [("test_01", "secret-ok")]
    assert session_manager.register_calls == [
        (
            "session-1",
            {
                "robot_id": "test_01",
                "bot_id": "xiaowen",
                "bot_name": "小文",
                "client_type": "rust",
                "is_new_robot": False,
            },
        )
    ]
    assert registration == {
        "robot_id": "test_01",
        "bot_id": "xiaowen",
        "bot_name": "小文",
        "client_type": "rust",
        "is_new_robot": False,
    }


def test_build_registered_message_payload():
    assert build_registered_message_payload(
        "session-1",
        {
            "robot_id": "test_01",
            "bot_id": "xiaowen",
            "bot_name": "小文",
            "client_type": "rust",
            "is_new_robot": False,
        },
    ) == {
        "session_id": "session-1",
        "robot_id": "test_01",
        "bot_id": "xiaowen",
        "bot_name": "小文",
        "client_type": "rust",
        "is_new_robot": False,
    }


def test_register_robot_session_rejects_bad_secret():
    runtime_state = _FakeRuntimeState(secret_ok=False)

    with pytest.raises(ValueError, match="robot_secret 校验失败"):
        asyncio.run(
            register_robot_session(
                session_manager=_FakeSessionManager(),
                runtime_state=runtime_state,
                session_id="session-1",
                payload={"robot_id": "test_01", "robot_secret": "bad"},
                robot_secret_required=True,
            )
        )


def test_resolve_request_bot_uses_registered_robot_binding():
    session_manager = _FakeSessionManager(registered=True, robot_id="test_01")
    runtime_state = _FakeRuntimeState()

    bot_id, bot_name = asyncio.run(
        resolve_request_bot(
            session_manager=session_manager,
            runtime_state=runtime_state,
            session_id="session-1",
            payload={},
        )
    )

    assert (bot_id, bot_name) == ("xiaowen", "小文")
    assert runtime_state.resolve_robot_calls == ["test_01"]
    assert session_manager.update_bot_calls == [
        ("session-1", {"bot_id": "xiaowen", "bot_name": "小文"})
    ]


def test_resolve_request_bot_uses_legacy_bot_when_session_not_registered():
    session_manager = _FakeSessionManager(registered=False)
    runtime_state = _FakeRuntimeState()

    bot_id, bot_name = asyncio.run(
        resolve_request_bot(
            session_manager=session_manager,
            runtime_state=runtime_state,
            session_id="session-1",
            payload={"bot_id": " legacy_bot "},
        )
    )

    assert (bot_id, bot_name) == ("legacy_bot", "Legacy Bot")
    assert runtime_state.resolve_legacy_calls == ["legacy_bot"]


class _FakeRuntimeState:
    def __init__(self, *, secret_ok: bool = True):
        self.secret_ok = secret_ok
        self.register_calls = []
        self.verify_calls = []
        self.resolve_robot_calls = []
        self.resolve_legacy_calls = []

    def register_robot(self, robot_id, client_type, *, allow_create):
        self.register_calls.append((robot_id, client_type, allow_create))
        return {
            "robot_id": robot_id,
            "assigned_bot_id": "xiaowen",
            "bot_name": "小文",
            "client_type": client_type,
            "enabled": True,
            "is_new": False,
            "created": False,
            "robot_secret_hash": "hash",
        }

    def verify_robot_secret(self, robot_id, robot_secret):
        self.verify_calls.append((robot_id, robot_secret))
        return self.secret_ok

    def resolve_robot_bot_binding(self, robot_id):
        self.resolve_robot_calls.append(robot_id)
        return {
            "assigned_bot_id": "xiaowen",
            "bot_name": "小文",
            "robot_enabled": True,
        }

    def resolve_legacy_bot(self, bot_id):
        self.resolve_legacy_calls.append(bot_id)
        return bot_id, "Legacy Bot"


class _FakeSessionManager:
    def __init__(self, *, registered: bool = False, robot_id: str | None = None):
        self.registered = registered
        self.robot_id = robot_id
        self.register_calls = []
        self.update_bot_calls = []

    def register_robot(self, session_id, **kwargs):
        self.register_calls.append((session_id, kwargs))
        return True

    def is_registered(self, session_id):
        return self.registered

    def get_robot_id(self, session_id):
        return self.robot_id

    def update_registered_bot(self, session_id, **kwargs):
        self.update_bot_calls.append((session_id, kwargs))
