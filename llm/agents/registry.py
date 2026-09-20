from __future__ import annotations

import importlib
import logging
import pkgutil

from llm.agent_runtime import AgentRegistry, BaseAgent
import llm.agents as agents_package

logger = logging.getLogger(__name__)


SKIP_MODULES = {"registry"}


def discover_local_agents() -> list[BaseAgent]:
    """Discover built-in Agent modules under llm.agents.

    A module is loadable when it exposes create_agent() -> BaseAgent. Import or
    validation errors are logged and skipped so one broken Agent does not stop
    the whole LLM service from starting.
    """
    agents: list[BaseAgent] = []
    package_path = list(getattr(agents_package, "__path__", []))
    package_prefix = f"{agents_package.__name__}."

    for module_info in sorted(pkgutil.iter_modules(package_path), key=lambda item: item.name):
        module_name = module_info.name
        if module_info.ispkg or module_name.startswith("_") or module_name in SKIP_MODULES:
            continue

        import_name = f"{package_prefix}{module_name}"
        try:
            module = importlib.import_module(import_name)
            factory = getattr(module, "create_agent", None)
            if factory is None:
                logger.debug("跳过 Agent 模块 %s：缺少 create_agent()", import_name)
                continue
            agent = factory()
            if not isinstance(agent, BaseAgent):
                raise TypeError(f"create_agent() returned {type(agent)!r}, expected BaseAgent")
            agents.append(agent)
        except Exception as exc:
            logger.exception("加载 Agent 模块失败: %s - %s", import_name, exc)

    return agents

def build_default_agent_registry() -> AgentRegistry:
    return AgentRegistry(discover_local_agents())
