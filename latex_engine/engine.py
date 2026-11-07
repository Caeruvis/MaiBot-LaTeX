"""回复保护和顺序替换；无发送副作用，重复钩子不会补发消息。"""

import asyncio
import time
import uuid
from collections import OrderedDict
from copy import deepcopy

from .parser import FAILURE, MAX_TEXT, Span, parse


class Engine:
    def __init__(self, renderer, *, automatic: bool = True):
        self.renderer = renderer
        self.automatic = automatic
        # token -> (过期时间, 会话, 公式)，总内存由条目数与解析长度双重限制。
        self.pending: OrderedDict[str, tuple[float, str, Span]] = OrderedDict()

    def prune(self) -> None:
        now = time.monotonic()
        for key, (expiry, _, _) in list(self.pending.items()):
            if expiry <= now:
                del self.pending[key]
        self.renderer.prune()

    def clear(self) -> None:
        self.pending.clear()
        self.renderer.clear()

    def protect(self, text: str, session_id: str) -> tuple[str, bool]:
        self.prune()
        output = []
        changed = False
        for span in parse(text):
            if span.kind in {"text", "token"}:
                output.append(span.content)
                changed |= span.kind == "token"
            elif span.kind == "math" and self.automatic:
                changed = True
                if len(span.content) > self.renderer.config.max_chars:
                    output.append(FAILURE)
                    continue
                token = "⟦MLTX:" + uuid.uuid4().hex + "⟧"
                self.pending[token] = (time.monotonic() + 180, session_id, span)
                while len(self.pending) > 512:
                    self.pending.popitem(last=False)
                output.append(token)
            else:
                changed = True
                output.append(FAILURE)
        return "".join(output), changed

    async def transform(
        self, segments: list[dict], session_id: str, *, manual: bool = False
    ) -> list[dict]:
        self.prune()
        if len(segments) > 256:
            return [{"type": "text", "data": FAILURE}]
        spans: list[Span | dict] = []
        run: list[str] = []

        def flush() -> None:
            if run:
                spans.extend(parse("".join(run)))
                run.clear()

        for segment in segments:
            if not isinstance(segment, dict):
                raise TypeError("invalid message")
            if segment.get("type") == "text":
                data = segment.get("data", "")
                if not isinstance(data, str):
                    raise TypeError("invalid text")
                run.append(data)
            else:
                flush()
                spans.append(deepcopy(segment))
        flush()
        resolved: list[Span | dict] = []
        count = 0
        for span in spans:
            if isinstance(span, dict):
                resolved.append(span)
                continue
            if span.kind == "token":
                entry = self.pending.get(span.content)
                span = entry[2] if entry and entry[1] == session_id else Span("error", "")
            if span.kind == "math":
                count += 1
                if count > self.renderer.config.max_formulas or not (self.automatic or manual):
                    span = Span("error", "")
            resolved.append(span)

        async def convert(span: Span | dict) -> dict:
            if isinstance(span, dict):
                return span
            if span.kind == "text":
                return {"type": "text", "data": span.content}
            if span.kind == "math":
                data = await self.renderer.render(span.content, span.display)
                if data:
                    return {"type": "image", "data": "", "binary_data_base64": data}
            return {"type": "text", "data": FAILURE}

        # gather 保持输入顺序；Renderer 控制全局并发和包含排队时间的超时。
        return await asyncio.gather(*(convert(span) for span in resolved))

    async def command(self, source: str) -> list[dict]:
        from .parser import unwrap

        if len(source) > MAX_TEXT:
            return [{"type": "text", "data": FAILURE}]
        source = source.strip()
        if source.startswith(("```", "~~~")):
            pieces = parse(source)
            if len(pieces) != 1 or pieces[0].kind != "math":
                return [{"type": "text", "data": FAILURE}]
            source = pieces[0].content
        data = await self.renderer.render(unwrap(source), True)
        if data:
            return [{"type": "image", "data": "", "binary_data_base64": data}]
        return [{"type": "text", "data": FAILURE}]
