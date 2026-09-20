"""
Bot 管理器

管理多个机器人的配置和状态
"""

import threading
from typing import Dict, Optional, List
from dataclasses import dataclass, field


@dataclass
class BotConfig:
    """机器人配置"""
    bot_id: str
    name: str
    system_prompt: str
    model: str = "qwen3-5-9b"
    temperature: float = 0.7
    max_tokens: int = 2000
    tts_profile_id: str = "default_tts_profile"
    max_response_chars: int = 0
    mcp_servers: List[str] = field(default_factory=list)
    agents: List[str] = field(default_factory=list)
    enabled: bool = True
    is_default: bool = False


class BotManager:
    """
    机器人管理器

    负责加载和管理 Bot 配置
    """

    DEFAULT_BOT_ID = "default"

    def __init__(self, initial_bots: Optional[Dict[str, BotConfig]] = None):
        """
        初始化 Bot 管理器
        """
        self._lock = threading.Lock()
        self._bots: Dict[str, BotConfig] = {}

        if initial_bots is not None:
            self.replace_all(initial_bots)
            default_bot = next((bot.bot_id for bot in initial_bots.values() if bot.is_default), None)
            if default_bot:
                self.DEFAULT_BOT_ID = default_bot

    def get_bot(self, bot_id: Optional[str] = None) -> BotConfig:
        """
        获取机器人配置

        Args:
            bot_id: 机器人 ID，为空时返回默认机器人

        Returns:
            BotConfig 对象

        Raises:
            KeyError: 机器人不存在
        """
        if not bot_id:
            bot_id = self.DEFAULT_BOT_ID

        with self._lock:
            if bot_id not in self._bots:
                raise KeyError(f"Bot '{bot_id}' not found")
            return self._bots[bot_id]

    def get_bot_or_default(self, bot_id: Optional[str] = None) -> BotConfig:
        """
        获取机器人配置，不存在时返回默认机器人

        Args:
            bot_id: 机器人 ID

        Returns:
            BotConfig 对象
        """
        try:
            return self.get_bot(bot_id)
        except KeyError:
            return self.get_bot(self.DEFAULT_BOT_ID)

    def list_bots(self) -> List[BotConfig]:
        """获取所有机器人配置"""
        with self._lock:
            return list(self._bots.values())

    def replace_all(self, bots: Dict[str, BotConfig]):
        """直接替换全部 Bot 配置。"""
        with self._lock:
            self._bots = dict(bots)
