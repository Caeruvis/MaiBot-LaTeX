"""在回复拆分前保护整段文字，发送时替换为一张图片。"""

import re
import time
import uuid
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass

from .parser import FAILURE, MAX_TEXT, TOKEN_RE, Span, parse, unwrap
from .render import MAX_CACHE_BYTES, math_to_latex, text_to_latex

_DOCUMENT = re.compile(r"(?:[ \t\r\n]|%[^\n]*\n)*\\documentclass\b")
_TEXT_ENVIRONMENT = re.compile(
    r"\\begin\s*\{(?:align\*?|alignat\*?|gather\*?|multline\*?|equation\*?|"
    r"tikzpicture|tabular\*?)\}"
)


@dataclass(frozen=True)
class Page:
    body: str
    full_document: bool = False


def _math_body(source: str, display: bool) -> str:
    if display and _TEXT_ENVIRONMENT.match(source.lstrip()):
        return "\n" + source + "\n"
    return math_to_latex(source, display)


def _parse_reply(text: str) -> list[Span]:
    if len(text) <= MAX_TEXT and _DOCUMENT.match(text):
        return [Span("math", text, display=True)]
    return parse(text)


def _image(data: str | None) -> dict:
    if data:
        return {"type": "image", "data": "", "binary_data_base64": data}
    return {"type": "text", "data": FAILURE}


class Engine:
    def __init__(self, renderer, *, automatic: bool = True):
        self.renderer = renderer
        self.automatic = automatic
        self.pending: OrderedDict[str, tuple[float, str, Page | str]] = OrderedDict()

    def prune(self) -> None:
        now = time.monotonic()
        for key, (expiry, _, _) in list(self.pending.items()):
            if expiry <= now:
                del self.pending[key]
        self.renderer.prune()

    def clear(self) -> None:
        self.pending.clear()
        self.renderer.clear()

    def _stash(self, session_id: str, page: Page | str) -> str:
        self.prune()
        token = "⟦MLTX:" + uuid.uuid4().hex + "⟧"
        self.pending[token] = (time.monotonic() + 180, session_id, page)
        # 源文本有字符上限，预渲染的 PNG 另受总字节数限制。
        image_bytes = sum(
            len(value) for _, _, value in self.pending.values() if isinstance(value, str)
        )
        while len(self.pending) > 512 or image_bytes > MAX_CACHE_BYTES:
            _, (_, _, value) = self.pending.popitem(last=False)
            if isinstance(value, str):
                image_bytes -= len(value)
        return token

    def _page(self, spans: list[Span]) -> Page | None:
        formulas = [span for span in spans if span.kind == "math"]
        if (
            any(span.kind not in {"text", "math"} for span in spans)
            or len(formulas) > self.renderer.config.max_formulas
            or any(len(span.content) > self.renderer.config.max_chars for span in formulas)
        ):
            return None
        if len(formulas) == 1 and _DOCUMENT.match(formulas[0].content):
            if all(span.kind == "math" or not span.content.strip() for span in spans):
                return Page(formulas[0].content, full_document=True)
            return None
        return Page(
            "".join(
                _math_body(span.content, span.display)
                if span.kind == "math"
                else text_to_latex(span.content)
                for span in spans
            )
        )

    def protect(self, text: str, session_id: str) -> tuple[str, bool]:
        if TOKEN_RE.fullmatch(text.strip()):
            return text.strip(), True
        spans = _parse_reply(text)
        if all(span.kind == "text" for span in spans):
            return text, False
        page = self._page(spans) if self.automatic and session_id else None
        return (self._stash(session_id, page) if page else FAILURE), True

    async def _render(self, page: Page | None) -> str | None:
        if page is None:
            return None
        return await self.renderer.render_body(page.body, full_document=page.full_document)

    async def _convert(self, text: str, session_id: str) -> dict:
        token = text.strip()
        if TOKEN_RE.fullmatch(token):
            entry = self.pending.get(token)
            if not entry or entry[1] != session_id:
                return _image(None)
            value = entry[2]
            return _image(value if isinstance(value, str) else await self._render(value))
        spans = _parse_reply(text)
        if all(span.kind == "text" for span in spans):
            return {"type": "text", "data": text}
        return _image(await self._render(self._page(spans) if self.automatic else None))

    async def transform(self, segments: list[dict], session_id: str) -> list[dict]:
        self.prune()
        if len(segments) > 256:
            return [_image(None)]
        output = []
        run = []
        for segment in segments:
            if not isinstance(segment, dict):
                raise TypeError("invalid message")
            if segment.get("type") == "text":
                data = segment.get("data", "")
                if not isinstance(data, str):
                    raise TypeError("invalid text")
                run.append(data)
                continue
            if run:
                output.append(await self._convert("".join(run), session_id))
                run.clear()
            output.append(deepcopy(segment))
        if run:
            output.append(await self._convert("".join(run), session_id))
        return output

    async def tool(self, source: str, format: str, session_id: str) -> str | None:
        if not session_id or not isinstance(source, str) or not 0 < len(source) <= MAX_TEXT:
            return None
        if format == "mixed":
            page = self._page(_parse_reply(source))
        elif format == "latex":
            page = Page(source, full_document=bool(_DOCUMENT.match(source)))
        else:
            return None
        image = await self._render(page)
        return self._stash(session_id, image) if image else None

    async def command(self, source: str) -> list[dict]:
        if len(source) > self.renderer.config.max_chars:
            return [_image(None)]
        source = source.strip()
        if source.startswith(("```", "~~~")):
            pieces = parse(source)
            if len(pieces) != 1 or pieces[0].kind != "math":
                return [_image(None)]
            source = pieces[0].content
        source = unwrap(source)
        if not source.strip():
            return [_image(None)]
        if _DOCUMENT.match(source):
            return [_image(await self._render(Page(source, full_document=True)))]
        return [_image(await self._render(Page(_math_body(source, True))))]
