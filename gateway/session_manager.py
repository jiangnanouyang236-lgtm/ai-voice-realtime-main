"""
会话管理器

管理 WebSocket 会话、对话历史
（唤醒状态和超时检测已移至客户端）
"""

import uuid
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, field
import logging

logger = logging.getLogger(__name__)


@dataclass
class Session:
    """会话数据"""
    session_id: str
    history: List[Dict] = field(default_factory=list)  # 对话历史
    interrupted: bool = False  # 打断标志
    robot_id: str | None = None
    bot_id: str | None = None
    bot_name: str | None = None
    client_type: str | None = None
    is_new_robot: bool = False
    last_active_at: float = field(default_factory=time.time)
    current_round_id: str | None = None
    current_playback_id: str | None = None
    current_round_started_at: float | None = None
    slow_ws_send_strikes: int = 0


class SessionManager:
    """会话管理器（线程安全）"""

    def __init__(self, max_history: int = 10):
        self.max_history = max_history
        self.sessions: Dict[str, Session] = {}
        self._lock = threading.Lock()
        logger.info(f"会话管理器已初始化: max_history={max_history}")

    def set_max_history(self, value: int) -> None:
        with self._lock:
            self.max_history = max(1, int(value))
        logger.info("更新会话历史长度限制: max_history=%s", self.max_history)

    def create_session(self) -> str:
        """创建新会话（线程安全）"""
        session_id = str(uuid.uuid4())
        with self._lock:
            self.sessions[session_id] = Session(session_id=session_id)
        logger.info(f"创建会话: {session_id}")
        return session_id

    def ensure_session(self, session_id: str) -> bool:
        """确保指定会话存在，用于内网网关复用上游 session_id。"""
        session_id = str(session_id or "").strip()
        if not session_id:
            return False
        with self._lock:
            if session_id not in self.sessions:
                self.sessions[session_id] = Session(session_id=session_id)
                logger.info(f"创建会话: {session_id}")
            else:
                self.sessions[session_id].last_active_at = time.time()
        return True

    def get_session(self, session_id: str) -> Optional[Session]:
        """获取会话（线程安全）"""
        with self._lock:
            return self.sessions.get(session_id)

    def add_message(self, session_id: str, role: str, content: str):
        """添加消息到历史（线程安全）"""
        with self._lock:
            session = self.sessions.get(session_id)
            if session:
                session.last_active_at = time.time()
                session.history.append({
                    "role": role,
                    "content": content,
                    "timestamp": datetime.now().astimezone().isoformat(),
                })

                # 限制历史长度
                if len(session.history) > self.max_history:
                    session.history = session.history[-self.max_history:]

                logger.debug(f"会话 {session_id} 添加消息: {role}")

    def get_history(self, session_id: str) -> List[Dict]:
        """获取对话历史（线程安全）"""
        with self._lock:
            session = self.sessions.get(session_id)
            return session.history.copy() if session else []

    def delete_session(self, session_id: str):
        """删除会话（线程安全）"""
        with self._lock:
            if session_id in self.sessions:
                del self.sessions[session_id]
                logger.info(f"删除会话: {session_id}")

    def get_stats(self) -> Dict:
        """获取统计信息（线程安全）"""
        with self._lock:
            registered_sessions = sum(1 for session in self.sessions.values() if session.robot_id)
            return {
                "total_sessions": len(self.sessions),
                "registered_sessions": registered_sessions,
            }

    def touch_session(self, session_id: str) -> bool:
        with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                return False
            session.last_active_at = time.time()
            return True

    def set_interrupted(self, session_id: str, value: bool = True):
        """设置打断标志（线程安全）"""
        with self._lock:
            session = self.sessions.get(session_id)
            if session:
                session.last_active_at = time.time()
                session.interrupted = value
                logger.info(f"会话 {session_id} 打断标志: {value}")

    def is_interrupted(self, session_id: str) -> bool:
        """检查是否被打断（线程安全）"""
        with self._lock:
            session = self.sessions.get(session_id)
            return session.interrupted if session else False

    def start_round(self, session_id: str, *, round_id: str, playback_id: str) -> bool:
        """标记当前正在处理的服务端轮次。"""
        with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                return False
            session.last_active_at = time.time()
            session.interrupted = False
            session.current_round_id = round_id
            session.current_playback_id = playback_id
            session.current_round_started_at = time.time()
            logger.info(
                "会话 %s 开始轮次: round_id=%s playback_id=%s",
                session_id,
                round_id,
                playback_id,
            )
            return True

    def cancel_current_round(self, session_id: str) -> dict[str, str] | None:
        """打断当前轮次，并返回需要通知客户端取消的播放标识。"""
        with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                return None
            session.last_active_at = time.time()
            session.interrupted = True
            logger.info(f"会话 {session_id} 打断标志: True")
            if not session.current_round_id:
                return None
            return {
                "round_id": session.current_round_id,
                "playback_id": session.current_playback_id or "",
            }

    def cancel_round_if_matches(
        self,
        session_id: str,
        *,
        round_id: str,
        playback_id: str,
    ) -> bool:
        """仅当客户端回执精确指向当前播放时打断，避免迟到回执误杀新轮次。"""
        round_id = str(round_id or "").strip()
        playback_id = str(playback_id or "").strip()
        if not round_id or not playback_id:
            return False
        with self._lock:
            session = self.sessions.get(session_id)
            if (
                not session
                or session.current_round_id != round_id
                or session.current_playback_id != playback_id
            ):
                return False
            session.last_active_at = time.time()
            session.interrupted = True
            logger.info(
                "会话 %s 按客户端播放回执打断当前轮次: round_id=%s playback_id=%s",
                session_id,
                round_id,
                playback_id,
            )
            return True

    def complete_round(self, session_id: str, round_id: str) -> bool:
        """仅当 round_id 仍是当前轮次时清理轮次标识。"""
        with self._lock:
            session = self.sessions.get(session_id)
            if not session or session.current_round_id != round_id:
                return False
            session.last_active_at = time.time()
            session.current_round_id = None
            session.current_playback_id = None
            session.current_round_started_at = None
            logger.info("会话 %s 完成轮次: round_id=%s", session_id, round_id)
            return True

    def is_current_round(self, session_id: str, round_id: str) -> bool:
        with self._lock:
            session = self.sessions.get(session_id)
            return bool(session and session.current_round_id == round_id)

    def get_current_round(self, session_id: str) -> dict[str, str] | None:
        with self._lock:
            session = self.sessions.get(session_id)
            if not session or not session.current_round_id:
                return None
            return {
                "round_id": session.current_round_id,
                "playback_id": session.current_playback_id or "",
            }

    def record_slow_ws_send(self, session_id: str) -> int:
        """记录一次慢 WebSocket 发送，返回当前连续慢发送次数。"""
        with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                return 0
            session.last_active_at = time.time()
            session.slow_ws_send_strikes += 1
            logger.warning(
                "会话 %s 慢 WebSocket 发送次数: %s",
                session_id,
                session.slow_ws_send_strikes,
            )
            return session.slow_ws_send_strikes

    def reset_slow_ws_send_strikes(self, session_id: str) -> None:
        with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                return
            if session.slow_ws_send_strikes:
                logger.info("会话 %s 清零慢 WebSocket 发送次数", session_id)
            session.slow_ws_send_strikes = 0

    def register_robot(
        self,
        session_id: str,
        *,
        robot_id: str,
        bot_id: str,
        bot_name: str,
        client_type: str | None,
        is_new_robot: bool,
    ) -> bool:
        """将机器人和 Bot 绑定到当前会话。"""
        with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                return False
            session.last_active_at = time.time()
            session.robot_id = robot_id
            session.bot_id = bot_id
            session.bot_name = bot_name
            session.client_type = client_type
            session.is_new_robot = is_new_robot
            return True

    def get_registered_bot(self, session_id: str) -> tuple[str | None, str | None]:
        """返回当前会话绑定的 Bot。"""
        with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                return None, None
            return session.bot_id, session.bot_name

    def get_robot_id(self, session_id: str) -> str | None:
        """返回当前会话绑定的 robot_id。"""
        with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                return None
            return session.robot_id

    def get_trace_context(self, session_id: str) -> Dict:
        """返回链路观测所需的轻量会话上下文。"""
        with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                return {
                    "robot_id": None,
                    "bot_id": None,
                    "bot_name": None,
                    "client_type": None,
                }
            return {
                "robot_id": session.robot_id,
                "bot_id": session.bot_id,
                "bot_name": session.bot_name,
                "client_type": session.client_type,
            }

    def update_registered_bot(self, session_id: str, *, bot_id: str, bot_name: str | None) -> bool:
        """更新当前会话上的 Bot 绑定，用于下次请求前重新同步 DB 中的最新关系。"""
        with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                return False
            session.last_active_at = time.time()
            session.bot_id = bot_id
            session.bot_name = bot_name
            return True

    def is_registered(self, session_id: str) -> bool:
        with self._lock:
            session = self.sessions.get(session_id)
            return bool(session and session.robot_id and session.bot_id)

    def cleanup_expired(self, idle_timeout_seconds: float = 1800) -> int:
        """清理长时间无活动的会话。"""
        now = time.time()
        with self._lock:
            expired_ids = [
                session_id
                for session_id, session in self.sessions.items()
                if now - session.last_active_at > idle_timeout_seconds
            ]
            for session_id in expired_ids:
                del self.sessions[session_id]

        if expired_ids:
            logger.info("清理过期会话 %s 个", len(expired_ids))
        return len(expired_ids)

    def list_sessions(self) -> List[Dict]:
        """返回当前会话快照，按最近活跃时间倒序排列。"""
        with self._lock:
            sessions = [
                {
                    "session_id": session.session_id,
                    "robot_id": session.robot_id,
                    "bot_id": session.bot_id,
                    "bot_name": session.bot_name,
                    "client_type": session.client_type,
                    "is_new_robot": session.is_new_robot,
                    "interrupted": session.interrupted,
                    "current_round_id": session.current_round_id,
                    "current_playback_id": session.current_playback_id,
                    "slow_ws_send_strikes": session.slow_ws_send_strikes,
                    "history_count": len(session.history),
                    "last_active_at": datetime.fromtimestamp(session.last_active_at).astimezone().isoformat(),
                    "registered": bool(session.robot_id and session.bot_id),
                }
                for session in self.sessions.values()
            ]

        return sorted(sessions, key=lambda item: item["last_active_at"], reverse=True)

    def get_session_detail(self, session_id: str) -> Optional[Dict]:
        """返回单个会话详情，包含当前进程内存中的最近对话历史。"""
        with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                return None
            history = [
                {
                    "role": str(message.get("role", "")),
                    "content": str(message.get("content", "")),
                    "timestamp": message.get("timestamp"),
                }
                for message in session.history
            ]
            return {
                "session_id": session.session_id,
                "robot_id": session.robot_id,
                "bot_id": session.bot_id,
                "bot_name": session.bot_name,
                "client_type": session.client_type,
                "is_new_robot": session.is_new_robot,
                "interrupted": session.interrupted,
                "current_round_id": session.current_round_id,
                "current_playback_id": session.current_playback_id,
                "slow_ws_send_strikes": session.slow_ws_send_strikes,
                "history_count": len(session.history),
                "last_active_at": datetime.fromtimestamp(session.last_active_at).astimezone().isoformat(),
                "registered": bool(session.robot_id and session.bot_id),
                "history": history,
            }
