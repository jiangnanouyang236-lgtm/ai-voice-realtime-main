"""
会话管理器

管理多轮对话的上下文和历史记录
"""

import logging
import threading
from typing import List, Dict, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class SessionManager:
    """会话管理器，管理对话历史"""

    def __init__(self, max_history: int = 10):
        """
        初始化会话管理器

        Args:
            max_history: 每个会话保留的最大历史消息数
        """
        self.sessions: Dict[str, List[Dict]] = {}
        self.agent_states: Dict[str, Dict] = {}
        self.max_history = max_history
        self._lock = threading.Lock()  # 添加线程锁
        logger.info(f"会话管理器已初始化，最大历史记录: {max_history}")

    def add_message(self, session_id: str, role: str, content: str):
        """
        添加消息到会话历史（线程安全）

        Args:
            session_id: 会话 ID
            role: 角色 (user/assistant/tool)
            content: 消息内容
        """
        with self._lock:
            if session_id not in self.sessions:
                self.sessions[session_id] = []

            message = {
                "role": role,
                "content": content,
                "timestamp": datetime.now().isoformat()
            }

            self.sessions[session_id].append(message)

            # 保持历史记录在限制范围内
            if len(self.sessions[session_id]) > self.max_history:
                # 保留系统消息和最近的消息
                self.sessions[session_id] = self.sessions[session_id][-self.max_history:]

            logger.debug(f"会话 {session_id} 添加消息: {role}")

    def add_tool_call(self, session_id: str, tool_name: str, tool_args: Dict, tool_result: str):
        """
        添加工具调用记录

        注意：不保存"调用工具"这样的技术信息，只保存工具结果
        这样可以避免 LLM 学习到错误的模式

        Args:
            session_id: 会话 ID
            tool_name: 工具名称
            tool_args: 工具参数
            tool_result: 工具结果
        """
        # 只添加工具结果消息（作为系统消息）
        self.add_message(session_id, "tool", f"[系统：工具 {tool_name} 返回结果]\n{tool_result}")

        logger.info(f"会话 {session_id} 记录工具调用: {tool_name}")

    def get_history(self, session_id: str) -> List[Dict]:
        """
        获取会话历史（线程安全）

        Args:
            session_id: 会话 ID

        Returns:
            历史消息列表
        """
        with self._lock:
            return self.sessions.get(session_id, []).copy()

    def get_messages_for_llm(self, session_id: str) -> List[Dict]:
        """
        获取格式化的消息历史，用于 LLM API

        Args:
            session_id: 会话 ID

        Returns:
            格式化的消息列表 [{"role": "user", "content": "..."}]
        """
        history = self.get_history(session_id)
        
        # 转换为 LLM API 格式
        messages = []
        for msg in history:
            # 工具消息转换为 user 角色（让 LLM 知道工具结果）
            role = "user" if msg["role"] == "tool" else msg["role"]
            messages.append({
                "role": role,
                "content": msg["content"]
            })
        
        return messages

    def clear_session(self, session_id: str) -> bool:
        """
        清除会话历史（线程安全）

        Args:
            session_id: 会话 ID

        Returns:
            是否实际清理了历史或 Agent 状态
        """
        with self._lock:
            had_session = session_id in self.sessions
            had_agent_state = session_id in self.agent_states
            if had_session:
                del self.sessions[session_id]
            if had_agent_state:
                del self.agent_states[session_id]
            if had_session or had_agent_state:
                logger.info(f"会话 {session_id} 已清除")
            return had_session or had_agent_state

    def get_agent_state(self, session_id: str) -> Optional[Dict]:
        """获取当前会话的轻量 Agent/Workflow 状态。"""
        with self._lock:
            state = self.agent_states.get(session_id)
            return dict(state) if state else None

    def set_agent_state(self, session_id: str, state: Dict):
        """设置当前会话的轻量 Agent/Workflow 状态。"""
        with self._lock:
            is_new = session_id not in self.agent_states
            self.agent_states[session_id] = dict(state)
            if is_new:
                logger.info(
                    "会话 %s 进入 Agent: %s",
                    session_id,
                    state.get("agent_id", "unknown"),
                )
            else:
                logger.debug(
                    "会话 %s 更新 Agent 状态: %s",
                    session_id,
                    state.get("agent_id", "unknown"),
                )

    def clear_agent_state(self, session_id: str):
        """清除当前会话的轻量 Agent/Workflow 状态。"""
        with self._lock:
            if session_id in self.agent_states:
                state = self.agent_states.pop(session_id)
                logger.info(
                    "会话 %s 退出 Agent: %s",
                    session_id,
                    state.get("agent_id", "unknown"),
                )

    def get_session_count(self) -> int:
        """获取当前会话数量（线程安全）"""
        with self._lock:
            return len(self.sessions)
