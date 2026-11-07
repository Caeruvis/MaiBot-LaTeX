import asyncio
import base64
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from latex_engine import render, texlive
from latex_engine.config import RenderConfig
from latex_engine.render import Renderer, math_to_latex, text_to_latex

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", 100, 80)


def test_display_math_trims_boundary_lines_and_keeps_comment_terminated():
    source = "\n  \n" + r"\frac{1}{2} % trailing comment" + "\n \n"
    assert math_to_latex(source, True) == "\n\\[\n\\frac{1}{2} % trailing comment\n\\]\n"


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
async def test_missing_native_dependencies_and_invalid_unicode_fail_safely(monkeypatch):
    renderer = Renderer(RenderConfig())

    async def unavailable(*args, **kwargs):
        raise FileNotFoundError("xelatex")

    monkeypatch.setattr(render, "compile_png", unavailable)
    assert await renderer.render_body("x") is None
    assert await renderer.render_body(chr(0xD800)) is None


def test_linux_worker_uses_local_tools_and_isolated_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(texlive, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(texlive, "_executable", lambda name, search: Path("/usr/bin") / name)
    job = texlive._Job(tmp_path)
    args = job._sandbox(job.xetex)
    assert args[0] == "/usr/bin/bwrap"
    assert "--unshare-all" in args and "--die-with-parent" in args
    assert args[args.index("--bind") + 1 : args.index("--bind") + 3] == [str(tmp_path), "/work"]
    assert args.count("--bind") == 1
    assert job.env["openin_any"] == job.env["openout_any"] == "p"
    assert job.env["TEXMFHOME"] == "/work/texmf-home"
