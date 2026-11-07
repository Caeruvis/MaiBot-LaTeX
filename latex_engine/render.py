"""在本机隔离环境中调用 TeX Live 和 Poppler 生成 PNG。"""

import asyncio
import base64
import hashlib
import struct
import time
from collections import OrderedDict

from .config import RenderConfig
from .parser import MAX_TEXT
from .texlive import compile_png

MAX_IMAGE_BYTES = 2_000_000
MAX_CACHE_BYTES = 24_000_000
MAX_DOCUMENT_BYTES = MAX_TEXT * 24

_TEXT_ESCAPES = str.maketrans(
    {
        "\\": r"\textbackslash{}",
        "{": r"\{",
        "}": r"\}",
        "$": r"\$",
        "&": r"\&",
        "#": r"\#",
        "%": r"\%",
        "_": r"\_",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
)


def text_to_latex(text: str) -> str:
    return text.translate(_TEXT_ESCAPES).replace("\n", "\n\\par\n")


def math_to_latex(source: str, display: bool) -> str:
    source = source.strip()
    # 分隔符前保留换行，避免末尾 TeX 注释吞掉闭合符。
    if display:
        return "\n\\[\n" + source + "\n\\]\n"
    return "\\(" + source + "\n\\)"


class Renderer:
    def __init__(self, config: RenderConfig):
        self.config = config
        self._slots = asyncio.Semaphore(config.concurrency)
        self._cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._cache_bytes = 0

    def clear(self) -> None:
        self._cache.clear()
        self._cache_bytes = 0

    def prune(self) -> None:
        now = time.monotonic()
        for key, (expiry, value) in list(self._cache.items()):
            if expiry <= now:
                del self._cache[key]
                self._cache_bytes -= len(value)

    def _document(self, body: str) -> str:
        config = self.config
        border = config.padding * 0.75
        width = min(640, config.max_width - 2 * config.padding) * 0.75
        size = config.font_size * 0.75
        return (
            rf"\documentclass[border={border}bp,varwidth={width}bp]{{standalone}}" + "\n"
            r"\usepackage{amsmath,amssymb,mathtools,xeCJK,xcolor,booktabs,siunitx,tikz}" + "\n"
            r"\setCJKmainfont{FandolSong-Regular.otf}" + "\n"
            rf"\definecolor{{foreground}}{{HTML}}{{{config.foreground[1:]}}}" + "\n"
            rf"\definecolor{{background}}{{HTML}}{{{config.background[1:]}}}" + "\n"
            r"\begin{document}\color{foreground}\pagecolor{background}" + "\n"
            rf"\fontsize{{{size}bp}}{{{size * 1.35}bp}}\selectfont" + "\n" + body + "\n"
            r"\end{document}" + "\n"
        )

    async def _compile(self, document: bytes, width: int, height: int) -> bytes:
        return await compile_png(
            document,
            round(96 * self.config.scale),
            width,
            height,
            MAX_IMAGE_BYTES,
        )

    async def render_body(self, source: str, *, full_document: bool = False) -> str | None:
        if not source.strip():
            return None
        try:
            document = (source if full_document else self._document(source)).encode()
        except UnicodeEncodeError:
            return None
        if len(document) > MAX_DOCUMENT_BYTES:
            return None
        key = hashlib.sha256(document).hexdigest()
        self.prune()
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key][1]

        width = round(self.config.max_width * self.config.scale)
        height = round(self.config.max_height * self.config.scale)
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                async with self._slots:
                    binary = await self._compile(document, width, height)
        except (OSError, TimeoutError):
            return None
        if (
            not 24 <= len(binary) <= MAX_IMAGE_BYTES
            or binary[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        ):
            return None
        image_width, image_height = struct.unpack(">II", binary[16:24])
        if not (0 < image_width <= width and 0 < image_height <= height):
            return None
        value = base64.b64encode(binary).decode("ascii")
        if self.config.cache_entries and self.config.cache_seconds:
            if key in self._cache:
                self._cache_bytes -= len(self._cache.pop(key)[1])
            self._cache[key] = (time.monotonic() + self.config.cache_seconds, value)
            self._cache_bytes += len(value)
            while (
                len(self._cache) > self.config.cache_entries or self._cache_bytes > MAX_CACHE_BYTES
            ):
                self._cache_bytes -= len(self._cache.popitem(last=False)[1][1])
        return value
