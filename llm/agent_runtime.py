"""
Minimal Agent runtime primitives.

The runtime keeps the shell stable while each Agent remains free to implement
its internals with rules, workflows, LLM calls, MCP tools, or plain functions.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from dataclasses import dataclass, field
import logging
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


AnswerNormalizer = Callable[[str], Awaitable[Optional[str]]]
AgentLLMResponder = Callable[
    [list[dict[str, str]], float, int],
    Awaitable[str],
]
AgentLLMStreamer = Callable[
    [list[dict[str, str]], float, int],
    AsyncIterator[str],
]


@dataclass(frozen=True)
class AgentToolResult:
    ok: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)


class AgentToolInvoker(ABC):
    @abstractmethod
    async def call(self, tool_name: str, arguments: dict[str, Any]) -> AgentToolResult:
        """Call a tool/function available to an Agent."""


@dataclass(frozen=True)
class AgentContext:
    session_id: str
    model_name: str
    robot_id: str | None = None
    llm_normalizer: AnswerNormalizer | None = None
    llm_responder: AgentLLMResponder | None = None
    llm_streamer: AgentLLMStreamer | None = None
    tool_invoker: AgentToolInvoker | None = None

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> AgentToolResult:
        if not self.tool_invoker:
            logger.warning("Agent tool invoker 未配置: %s args=%s", tool_name, arguments)
            return AgentToolResult(
                ok=False,
                message="tool invoker unavailable",
                data={"tool_name": tool_name},
            )
        return await self.tool_invoker.call(tool_name, arguments)

    def fire_and_forget_tool(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        """Schedule a tool call without delaying the conversational response."""
        if not self.tool_invoker:
            logger.warning("Agent background tool invoker 未配置: %s args=%s", tool_name, arguments)
            return False

        async def runner():
            return await self.tool_invoker.call(tool_name, arguments)

        try:
            task = asyncio.create_task(runner())
        except RuntimeError:
            logger.exception("Agent background tool 调度失败: %s", tool_name)
            return False

        def on_done(done_task: asyncio.Task):
            try:
                result = done_task.result()
            except Exception:
                logger.exception("Agent background tool 异常: %s", tool_name)
                return
            if result.ok:
                logger.debug("Agent background tool 完成: %s message=%s", tool_name, result.message)
            else:
                logger.warning("Agent background tool 失败: %s message=%s", tool_name, result.message)

        task.add_done_callback(on_done)
        return True

    async def ask_llm(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 120,
    ) -> str | None:
        if not self.llm_responder:
            logger.warning("Agent LLM responder 未配置")
            return None
        return await self.llm_responder(messages, temperature, max_tokens)

    async def ask_llm_stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 120,
    ) -> AsyncIterator[str]:
        if self.llm_streamer:
            async for chunk in self.llm_streamer(messages, temperature, max_tokens):
                yield chunk
            return

        response = await self.ask_llm(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if response:
            yield response


@dataclass(frozen=True)
class AgentResult:
    text: str
    state: dict[str, Any] | None = None
    finished: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentStreamEvent:
    text: str = ""
    result: AgentResult | None = None


class BaseAgent(ABC):
    id: str
    name: str
    description: str = ""
    enabled_by_default: bool = True
    trigger_examples: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    tool_groups: tuple[str, ...] = ()

    @abstractmethod
    def can_enter(self, text: str, context: AgentContext) -> bool:
        """Return True when this Agent should start handling the user input."""

    @abstractmethod
    async def start(self, text: str, context: AgentContext) -> AgentResult:
        """Start the Agent and return its first response."""

    @abstractmethod
    async def handle(
        self,
        text: str,
        state: dict[str, Any],
        context: AgentContext,
    ) -> AgentResult:
        """Handle input while this Agent is active."""


class AgentRegistry:
    def __init__(self, agents: list[BaseAgent] | None = None):
        self._agents: dict[str, BaseAgent] = {}
        if agents:
            for agent in agents:
                self.register(agent)

    def register(self, agent: BaseAgent):
        if agent.id in self._agents:
            raise ValueError(f"Agent already registered: {agent.id}")
        self._agents[agent.id] = agent

    def get(self, agent_id: str) -> BaseAgent | None:
        return self._agents.get(agent_id)

    def find_entry_agent(
        self,
        text: str,
        context: AgentContext,
        *,
        allowed_agent_ids: set[str] | None = None,
    ) -> BaseAgent | None:
        for agent in self._agents.values():
            if allowed_agent_ids is not None and agent.id not in allowed_agent_ids:
                continue
            if agent.can_enter(text, context):
                return agent
        return None

    def list_agents(self) -> list[BaseAgent]:
        return list(self._agents.values())
