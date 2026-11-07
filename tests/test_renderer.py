import asyncio
import base64
import struct

import pytest

from latex_engine.config import RenderConfig
from latex_engine.render import Renderer, text_to_latex

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", 100, 80)


@pytest.mark.asyncio
async def test_xelatex_document_and_cache(monkeypatch):
    renderer = Renderer(RenderConfig())
    requests = []

    async def compile_document(document, width, height):
        requests.append(document.decode())
        assert (width, height) == (3200, 2400)
        return PNG

    monkeypatch.setattr(renderer, "_compile", compile_document)
    source = text_to_latex(r"价格 $5，命令 \input{file}。") + r"\[\qty{2}{\meter}\]"
    assert await renderer.render_body(source) == base64.b64encode(PNG).decode()
    assert await renderer.render_body(source) == base64.b64encode(PNG).decode()
    assert len(requests) == 1
    assert r"\textbackslash{}input\{file\}" in requests[0]
    assert r"价格 \$5" in requests[0]
    assert r"\qty{2}{\meter}" in requests[0]


@pytest.mark.asyncio
async def test_complete_document_bypasses_template(monkeypatch):
    renderer = Renderer(RenderConfig())
    source = r"\documentclass{standalone}\begin{document}x\end{document}"

    async def compile_document(document, width, height):
        assert document == source.encode()
        return PNG

    monkeypatch.setattr(renderer, "_compile", compile_document)
    assert await renderer.render_body(source, full_document=True)


@pytest.mark.parametrize("binary", [b"bad", PNG[:16] + struct.pack(">II", 3201, 80)])
@pytest.mark.asyncio
async def test_rejects_invalid_png(monkeypatch, binary):
    renderer = Renderer(RenderConfig())

    async def compile_document(*args):
        return binary

    monkeypatch.setattr(renderer, "_compile", compile_document)
    assert await renderer.render_body("x") is None


@pytest.mark.asyncio
async def test_timeout_includes_queue(monkeypatch):
    renderer = Renderer(RenderConfig(concurrency=1, timeout_seconds=1))

    async def compile_document(*args):
        pytest.fail("queued render must not start")

    monkeypatch.setattr(renderer, "_compile", compile_document)
    async with renderer._slots:
        assert await renderer.render_body("x") is None


@pytest.mark.asyncio
async def test_cancellation_propagates(monkeypatch):
    renderer = Renderer(RenderConfig())
    started = asyncio.Event()

    async def compile_document(*args):
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(renderer, "_compile", compile_document)
    task = asyncio.create_task(renderer.render_body("x"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not renderer._cache


@pytest.mark.asyncio
async def test_missing_docker_and_invalid_unicode_fail_safely(monkeypatch):
    renderer = Renderer(RenderConfig())

    async def unavailable(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unavailable)
    assert await renderer.render_body("x") is None
    assert await renderer.render_body(chr(0xD800)) is None


@pytest.mark.asyncio
async def test_docker_run_is_isolated_and_does_not_pull(monkeypatch):
    renderer = Renderer(RenderConfig())
    writes = []

    class Process:
        returncode = 0

        class stdin:
            write = writes.append

            @staticmethod
            async def drain():
                pass

            @staticmethod
            def close():
                pass

        class stdout:
            @staticmethod
            async def readexactly(size):
                raise asyncio.IncompleteReadError(PNG, size)

        async def wait(self):
            return self.returncode

    async def start(*args, **kwargs):
        assert args[:2] == ("docker", "run")
        assert {"--network=none", "--read-only", "--cap-drop=ALL", "--pull=never"} <= set(args)
        assert not any(arg.startswith(("-v", "--volume", "--mount")) for arg in args[2:])
        assert "formula" not in " ".join(args)
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    assert await renderer._compile(b"formula", 3200, 2400) == PNG
    assert writes == [b"formula"]
