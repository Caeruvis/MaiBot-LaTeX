"""离线 KaTeX + Host render.html2png；公式仅以 JSON 数据传入脚本。"""

import asyncio
import base64
import hashlib
import json
import re
import struct
import time
from collections import OrderedDict
from pathlib import Path

from .config import RenderConfig

VERSION = "katex-0.16.22-layout-1"
MAX_IMAGE_BYTES = 2_000_000
MAX_CACHE_BYTES = 24_000_000


def _script_json(value: object) -> str:
    # </script> 与 Unicode 行分隔符不能结束 JSON 数据块。
    return (
        json.dumps(value, ensure_ascii=True)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


class Renderer:
    def __init__(self, capability, config: RenderConfig, vendor: Path):
        self.capability = capability
        self.config = config
        self.vendor = vendor
        self._assets: tuple[str, str] | None = None
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

    def _load_assets(self) -> tuple[str, str]:
        if self._assets is None:
            js = (self.vendor / "dist/katex.min.js").read_text()
            css = (self.vendor / "dist/katex.min.css").read_text()

            def inline_font(match: re.Match) -> str:
                name = match.group(1)
                data = (self.vendor / "dist/fonts" / name).read_bytes()
                return (
                    "src:url(data:font/woff2;base64,"
                    + base64.b64encode(data).decode()
                    + ') format("woff2")'
                )

            css = re.sub(r"src:url\(fonts/([^()]+\.woff2)\).*?(?=;|})", inline_font, css)
            self._assets = js, css
        return self._assets

    def build_html(self, source: str, display: bool) -> str:
        js, css = self._load_assets()
        cfg = self.config
        data = _script_json(
            {
                "source": source,
                "display": display,
                "width": cfg.max_width,
                "height": cfg.max_height,
            }
        )
        # trust 回调必须抛出：单纯 trust=false 会把受禁命令以红色源码显示。
        bootstrap = r"""
        (async () => {
          const data = JSON.parse(document.getElementById('input').textContent);
          const box = document.getElementById('formula');
          try {
            katex.render(data.source, box, {
              displayMode: data.display, output: 'html', throwOnError: true,
              strict: () => { throw new Error('unsupported'); },
              trust: () => { throw new Error('untrusted'); },
              maxExpand: 500, maxSize: Infinity, macros: {}
            });
            await document.fonts.ready;
            // 等待字体布局；长式保持完整，超尺寸直接失败。
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            const rect = box.getBoundingClientRect();
            if (!rect.width || !rect.height || rect.width > data.width || rect.height > data.height
                || box.querySelector('.katex-error')) throw new Error('size');
            box.dataset.ready = 'true';
          } catch (_) {
            box.replaceChildren();
            // 用一像素空图返回失败信号，避免对不存在的选择器等到超时。
            box.style.cssText = 'display:block;width:1px;height:1px;padding:0;margin:0;overflow:hidden';
            box.dataset.ready = 'true';
          }
        })();
        """
        return (
            '<!doctype html><html><head><meta charset="utf-8"><style>'
            + css
            + f"html,body{{margin:0;background:{cfg.background};}}"
            + f"#formula{{display:inline-block;width:max-content;padding:{cfg.padding}px;"
            + f"font-size:{cfg.font_size}px;color:{cfg.foreground};background:{cfg.background};}}"
            + ".katex-display{margin:0}.katex{white-space:nowrap}"
            + '</style></head><body><div id="formula"></div>'
            + '<script type="application/json" id="input">'
            + data
            + "</script>"
            + "<script>"
            + js
            + "</script><script>"
            + bootstrap
            + "</script></body></html>"
        )

    async def render(self, source: str, display: bool = True) -> str | None:
        if not source.strip() or len(source) > self.config.max_chars:
            return None
        config_json = self.config.model_dump_json()
        key = hashlib.sha256((VERSION + config_json + str(display) + source).encode()).hexdigest()
        self.prune()
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key][1]
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                async with self._slots:
                    # 大字体资源只在初次使用读取；不阻塞事件循环。
                    html = await asyncio.to_thread(self.build_html, source, display)
                    result = await self.capability.html2png(
                        html,
                        selector='#formula[data-ready="true"]',
                        wait_for_selector='#formula[data-ready="true"]',
                        viewport={
                            "width": self.config.max_width,
                            "height": self.config.max_height,
                        },
                        device_scale_factor=self.config.scale,
                        allow_network=False,
                        render_timeout_ms=int(self.config.timeout_seconds * 1000) - 100,
                    )
                    if not isinstance(result, dict) or result.get("mime_type") != "image/png":
                        return None
                    value = result.get("image_base64", "")
                    if not isinstance(value, str) or len(value) > MAX_IMAGE_BYTES * 4 // 3 + 8:
                        return None
                    binary = base64.b64decode(value, validate=True)
                    if len(binary) < 24 or binary[:8] != b"\x89PNG\r\n\x1a\n":
                        return None
                    width, height = struct.unpack(">II", binary[16:24])
                    if not (
                        8 < width <= self.config.max_width * self.config.scale + 2
                        and 8 < height <= self.config.max_height * self.config.scale + 2
                    ):
                        return None
        except Exception:
            # 渲染服务异常常含公式源码；不记录、不附带给平台。
            return None
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
