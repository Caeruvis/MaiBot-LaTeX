import asyncio
import base64
import struct
from pathlib import Path

from latex_engine.config import RenderConfig
from latex_engine.render import Renderer

VENDOR = Path(__file__).resolve().parents[1] / "vendor/katex"
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 100, 60)).decode()


class Capability:
    def __init__(self):
        self.calls = []
        self.delay = 0
        self.error = False

    async def html2png(self, html, **kwargs):
        self.calls.append((html, kwargs))
        await asyncio.sleep(self.delay)
        if self.error:
            raise RuntimeError("private source and /private/path")
        return {"image_base64": PNG, "mime_type": "image/png"}


def test_render_uses_no_network_and_caches_by_source_mode_and_config():
    async def run():
        cap = Capability()
        renderer = Renderer(cap, RenderConfig(), VENDOR)
        assert await renderer.render("x", True) == PNG
        assert await renderer.render("x", True) == PNG
        assert len(cap.calls) == 1
        await renderer.render("x", False)
        await renderer.render("y", True)
        renderer.config = RenderConfig(font_size=32)
        await renderer.render("x", True)
        assert len(cap.calls) == 4
        assert all(kw["allow_network"] is False for _, kw in cap.calls)

    asyncio.run(run())


def test_failures_do_not_return_error_or_source():
    async def run():
        cap = Capability()
        cap.error = True
        assert await Renderer(cap, RenderConfig(), VENDOR).render("private") is None

    asyncio.run(run())


def test_timeout_includes_queue():
    async def run():
        cap = Capability()
        cap.delay = 2
        renderer = Renderer(cap, RenderConfig(timeout_seconds=1, concurrency=1), VENDOR)
        start = asyncio.get_running_loop().time()
        results = await asyncio.gather(*(renderer.render(str(i)) for i in range(4)))
        assert results == [None] * 4
        assert asyncio.get_running_loop().time() - start < 1.5
        assert len(cap.calls) == 1

    asyncio.run(run())


def test_cancellation_propagates():
    async def run():
        cap = Capability()
        cap.delay = 5
        task = asyncio.create_task(Renderer(cap, RenderConfig(), VENDOR).render("x"))
        await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return
        assert False, "cancel swallowed"

    asyncio.run(run())


def test_missing_dependency_and_oversize_input_fail_safely(tmp_path):
    async def run():
        cap = Capability()
        renderer = Renderer(cap, RenderConfig(), tmp_path)
        assert await renderer.render("x") is None
        assert await renderer.render("x" * 6001) is None
        assert not cap.calls

    asyncio.run(run())


def test_assets_are_offline_and_formula_is_data():
    html = Renderer(None, RenderConfig(), VENDOR).build_html(r"\text{</script><img src=x>}", True)
    assert "<img src=x>" not in html
    assert "fonts/KaTeX_" not in html
    assert "data:font/woff2;base64," in html
    assert "throwOnError: true" in html
    assert "throw new Error('untrusted')" in html


def test_lru_and_ttl():
    async def run():
        cap = Capability()
        renderer = Renderer(cap, RenderConfig(cache_entries=1), VENDOR)
        await renderer.render("a")
        await renderer.render("b")
        await renderer.render("a")
        assert len(cap.calls) == 3
        for k, (_, v) in list(renderer._cache.items()):
            renderer._cache[k] = (0, v)
        renderer.prune()
        assert not renderer._cache and renderer._cache_bytes == 0

    asyncio.run(run())
