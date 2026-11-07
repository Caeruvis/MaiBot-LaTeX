"""Caeruvis 的 MaiBot LaTeX 插件，目标 Host 1.3.19 / SDK 2.21.0。"""

import asyncio
import re
import time
from collections import OrderedDict
from contextlib import suppress
from copy import deepcopy
from pathlib import Path

from maibot_sdk import Command, HookHandler, MaiBotPlugin
from maibot_sdk.types import ErrorPolicy, HookOrder

from .latex_engine.config import LatexConfig
from .latex_engine.engine import Engine
from .latex_engine.guard import receipt
from .latex_engine.parser import FAILURE
from .latex_engine.render import Renderer

COMMAND_PATTERN = r"(?s)^/(?:latex|tex|公式)(?:\s+(?P<source>.*))?\s*$"
MAX_PENDING_COMMANDS = 32


class LatexPlugin(MaiBotPlugin):
    config_model = LatexConfig

    def __init__(self):
        super().__init__()
        self.engine = None
        self._cleanup = None
        self._commands: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._command_locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self._pending_commands = 0

    async def on_load(self) -> None:
        self._configure()
        self._cleanup = asyncio.create_task(self._maintenance())

    def _configure(self) -> None:
        settings = self.config
        if self.engine is not None:
            self.engine.clear()
        self.engine = Engine(
            Renderer(self.ctx.render, settings.render, Path(__file__).parent / "vendor/katex"),
            automatic=settings.plugin.auto_render,
        )

    async def _maintenance(self) -> None:
        while True:
            await asyncio.sleep(30)
            self.engine.prune()
            now = time.monotonic()
            self._commands = OrderedDict((k, v) for k, v in self._commands.items() if v > now)

    async def on_unload(self) -> None:
        if self._cleanup:
            self._cleanup.cancel()
            with suppress(asyncio.CancelledError):
                await self._cleanup
        if self.engine:
            self.engine.clear()

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        # Runner 已先应用经过 config_model 校验的配置。
        self._configure()

    @HookHandler(
        "maisaka.reply.before_post_process",
        name="protect_formulas",
        description="完整回复拆分前隐藏公式源码",
        order=HookOrder.LATE,
        timeout_ms=2000,
        error_policy=ErrorPolicy.ABORT,
    )
    async def protect_formulas(self, **kwargs):
        updated = dict(kwargs)
        try:
            response, changed = self.engine.protect(
                str(kwargs.get("response") or ""), str(kwargs.get("session_id") or "")
            )
        except Exception:
            response, changed = FAILURE, True
        updated["response"] = response
        if changed:
            updated.update(skip_post_process=True, enable_splitter=False, enable_chinese_typo=False)
        value = {
            key: updated[key]
            for key in (
                "response",
                "skip_post_process",
                "enable_splitter",
                "enable_chinese_typo",
            )
        }
        return {
            "action": "continue",
            "modified_kwargs": updated,
            "custom_result": receipt("reply", value),
        }

    @HookHandler(
        "send_service.before_send",
        name="render_and_guard",
        description="平台发送前将公式换成图片并校验保护回执",
        order=HookOrder.LATE,
        timeout_ms=15000,
        error_policy=ErrorPolicy.ABORT,
    )
    async def render_and_guard(self, **kwargs):
        # 此钩子拥有原消息的发送权，只返回改写结果，绝不在钩子内重发。
        updated = dict(kwargs)
        message = deepcopy(kwargs["message"])
        original = message["raw_message"]
        if not isinstance(original, list):
            raise ValueError("invalid message")
        segments = await self.engine.transform(original, str(message.get("session_id") or ""))
        message["raw_message"] = segments
        if segments != original:
            message["processed_plain_text"] = "".join(
                str(s.get("data", ""))
                if s.get("type") == "text"
                else "[公式图片]"
                if s.get("type") == "image"
                else ""
                for s in segments
            )
            message["is_picture"] = any(s.get("type") == "image" for s in segments)
        updated["message"] = message
        return {
            "action": "continue",
            "modified_kwargs": updated,
            "custom_result": receipt("send", message),
        }

    @Command(
        "latex",
        description="直接渲染命令后面的 LaTeX 数学代码；所有用户可用",
        pattern=COMMAND_PATTERN,
    )
    async def latex_command(self, **kwargs):
        message = kwargs.get("message") or {}
        stream = str(kwargs.get("stream_id") or message.get("session_id") or "")
        source = (kwargs.get("matched_groups") or {}).get("source")
        if source is None:
            match = re.fullmatch(COMMAND_PATTERN, str(kwargs.get("raw_message") or ""))
            source = match.group("source") if match else ""
        if not stream:
            return False, "无法确定会话", 2
        message_id = str(message.get("message_id") or "")
        key = (stream, message_id)
        if message_id and self._commands.get(key, 0) > time.monotonic():
            return True, "该命令已处理", 2
        if message_id:
            self._commands[key] = time.monotonic() + 180
            while len(self._commands) > 512:
                self._commands.popitem(last=False)
        if self._pending_commands >= MAX_PENDING_COMMANDS:
            return False, "公式渲染繁忙", 2
        lock, users = self._command_locks.get(stream, (asyncio.Lock(), 0))
        self._command_locks[stream] = (lock, users + 1)
        self._pending_commands += 1
        try:
            async with asyncio.timeout(25):
                async with lock:
                    if not str(source or "").strip():
                        await self.ctx.send.text("用法：/latex 后跟数学公式代码。", stream)
                        return True, "已显示用法", 2
                    segments = await self.engine.command(str(source))
                    hybrid = [
                        {
                            "type": s["type"],
                            "content": s.get("binary_data_base64") or s.get("data", ""),
                        }
                        for s in segments
                    ]
                    result = await self.ctx.send.hybrid(hybrid, stream, return_details=True)
                    sent = result.get("sent", False) if isinstance(result, dict) else result is True
                    if not sent:
                        # 不重试可能已经部分送达的图文；仅尝试发送无源码的短提示。
                        await self.ctx.send.text(FAILURE, stream)
                    return bool(sent), "公式已处理" if sent else "公式发送失败", 2
        except Exception:
            return False, "公式暂时无法显示", 2
        finally:
            self._pending_commands -= 1
            current_lock, users = self._command_locks[stream]
            if users == 1:
                del self._command_locks[stream]
            else:
                self._command_locks[stream] = (current_lock, users - 1)


def create_plugin():
    return LatexPlugin()
