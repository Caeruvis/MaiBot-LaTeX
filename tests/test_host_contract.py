"""执行目标 Host 的实际函数体；网络、模型、IPC 和适配器均用替身。"""

import ast
import asyncio
import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from copy import deepcopy
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from latex_engine import guard
from tests.test_plugin import make_plugin, message
from tools.build_host_patch import build

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get("MAIBOT_SOURCE", "/missing-maibot-source"))
pytestmark = pytest.mark.skipif(
    not SOURCE.is_dir(), reason="设置 MAIBOT_SOURCE 指向核实的 1.3.19 源码"
)


@pytest.fixture(scope="module")
def patched(tmp_path_factory):
    root = tmp_path_factory.mktemp("patched-host")
    files = [
        "src/maisaka/builtin_tool/reply.py",
        "src/services/send_service.py",
        "src/plugin_runtime/runner/manifest_validator.py",
    ]
    for name in files:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / name, target)
    patch = root / "strict.patch"
    build(SOURCE, patch)
    result = subprocess.run(
        ["patch", "--batch", "-p1", "-i", str(patch)],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return root


def functions(path, names, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    tree.body = [
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    ] + [
        n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in names
    ]
    exec(compile(ast.fix_missing_locations(tree), str(path), "exec"), namespace)
    return namespace


@pytest.mark.parametrize("mode", ["ok", "missing", "error", "tampered", "exception"])
def test_actual_reply_hook_requires_receipt(patched, monkeypatch, mode):
    monkeypatch.setitem(sys.modules, "src.services.latex_guard", guard)

    async def run():
        plugin = make_plugin()

        async def invoke_hook(name, **kwargs):
            if mode == "exception":
                raise TimeoutError()
            response = await plugin.protect_formulas(**kwargs)
            updated = response["modified_kwargs"] if mode != "missing" else kwargs
            if mode == "tampered":
                updated["response"] += "$injected$"
            return SimpleNamespace(
                kwargs=updated,
                custom_results=[] if mode == "missing" else [response["custom_result"]],
                aborted=False,
                errors=["timeout"] if mode == "error" else [],
            )

        runtime = SimpleNamespace(invoke_hook=invoke_hook)
        tool_ctx = SimpleNamespace(
            get_runtime_manager=lambda: runtime,
            runtime=SimpleNamespace(session_id="s1"),
        )
        ns = functions(
            patched / "src/maisaka/builtin_tool/reply.py",
            {"_invoke_before_post_process_hook", "_require_hook_bool"},
            {},
        )
        if mode == "exception":
            with pytest.raises(TimeoutError):
                await ns["_invoke_before_post_process_hook"](tool_ctx, "$secret$", "m1", {})
        else:
            response, _ = await ns["_invoke_before_post_process_hook"](
                tool_ctx, "$secret$", "m1", {}
            )
            assert "secret" not in response
            assert bool(response) == (mode == "ok")

    asyncio.run(run())


def test_reply_caller_stops_on_hook_exception(patched):
    tree = ast.parse(
        (patched / "src/maisaka/builtin_tool/reply.py").read_text(encoding="utf-8-sig")
    )
    handler = next(
        n.handlers[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.Try)
        and n.handlers
        and any(
            isinstance(c, ast.Call)
            and isinstance(c.func, ast.Name)
            and c.func.id == "_invoke_before_post_process_hook"
            for s in n.body
            for c in ast.walk(s)
        )
    )
    module = ast.Module(body=handler.body, type_ignores=[])
    ns = {
        "reply_text": "$secret$",
        "logger": logging.getLogger("test"),
        "exc": TimeoutError(),
    }
    exec(
        compile(ast.fix_missing_locations(module), "actual-reply-exception-branch", "exec"),
        ns,
    )
    assert ns["reply_text"] == ""


@pytest.mark.parametrize("mode", ["ok", "missing", "error", "tampered", "invalid_payload"])
def test_actual_send_hook_stops_bad_or_missing_transform(patched, monkeypatch, mode):
    monkeypatch.setitem(sys.modules, "src.services.latex_guard", guard)

    async def run():
        plugin = make_plugin()

        async def invoke_hook(name, **kwargs):
            response = await plugin.render_and_guard(**kwargs)
            updated = response["modified_kwargs"] if mode != "missing" else kwargs
            receipts = [] if mode == "missing" else [response["custom_result"]]
            if mode == "tampered":
                updated["message"]["raw_message"].append({"type": "text", "data": "$injected$"})
            return SimpleNamespace(
                kwargs=updated,
                custom_results=receipts,
                errors=["failed"] if mode == "error" else [],
                aborted=False,
            )

        def deserialize(msg):
            if mode == "invalid_payload":
                raise ValueError("bad payload")
            return deepcopy(msg)

        ns = functions(
            patched / "src/services/send_service.py",
            {"_invoke_send_hook"},
            {
                "serialize_session_message": deepcopy,
                "deserialize_session_message": deserialize,
                "_get_runtime_manager": lambda: SimpleNamespace(invoke_hook=invoke_hook),
                "logger": logging.getLogger("test"),
            },
        )
        result, outgoing = await ns["_invoke_send_hook"](
            "send_service.before_send", message("前 $secret$ 后")
        )
        assert result.aborted == (mode != "ok")
        if not result.aborted:
            assert outgoing["raw_message"][1]["type"] == "image"
            assert "secret" not in outgoing["processed_plain_text"]

    asyncio.run(run())


@pytest.mark.parametrize("sent", [False, True])
def test_actual_platform_send_does_not_retry_failed_image(patched, sent):
    async def run():
        msg = SimpleNamespace(message_id="safe1")
        batch = SimpleNamespace(has_success=sent, sent_receipts=[])
        platform = SimpleNamespace(
            ensure_send_pipeline_ready=AsyncMock(),
            build_route_key_from_message=lambda _: "route",
            send_message=AsyncMock(return_value=batch),
        )

        async def hook(name, message, **kwargs):
            return SimpleNamespace(aborted=False, kwargs=kwargs), message

        ns = functions(
            patched / "src/services/send_service.py",
            {"_send_via_platform_io", "_coerce_bool"},
            {
                "_invoke_send_hook": hook,
                "get_platform_io_manager": lambda: platform,
                "_prepare_message_for_platform_io": AsyncMock(),
                "_apply_successful_delivery_receipt": AsyncMock(),
                "_dispatch_adapter_callbacks": AsyncMock(),
                "_record_sent_emoji_usage": lambda _: None,
                "_store_sent_message": AsyncMock(),
                "_notify_memory_automation_on_message_sent": AsyncMock(),
                "_log_platform_io_failures": lambda _: None,
                "logger": logging.getLogger("test"),
            },
        )
        result = await ns["_send_via_platform_io"](
            msg,
            typing=False,
            set_reply=False,
            reply_message_id=None,
            storage_message=False,
            show_log=False,
        )
        assert (result is not None) == sent
        assert platform.send_message.await_count == 1

    asyncio.run(run())


def test_host_patch_preserves_required_manifest_urls(patched, monkeypatch):
    relative_path = "src/plugin_runtime/runner/manifest_validator.py"
    path = patched / relative_path
    assert path.read_bytes() == (SOURCE / relative_path).read_bytes()
    tree = ast.parse(path.read_text())
    tree.body = [
        n
        for n in tree.body
        if not (isinstance(n, ast.ImportFrom) and (n.level or (n.module or "").startswith("src.")))
    ]
    module = ModuleType("test_host_manifest_models")
    module.__dict__["get_logger"] = lambda _: logging.getLogger("test")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(tree, str(path), "exec"), module.__dict__)
    data = json.loads((ROOT / "_manifest.json").read_text())
    data["author"]["url"] = "https://example.invalid/author"
    data["urls"]["repository"] = "https://example.invalid/repository"
    module.PluginManifest.model_validate(data)
    for field, key in (("author", "url"), ("urls", "repository")):
        missing_url = deepcopy(data)
        missing_url[field][key] = ""
        with pytest.raises(ValueError):
            module.PluginManifest.model_validate(missing_url)


@pytest.mark.parametrize("allow_abort", [False, True])
def test_real_dispatcher_error_policy_confirms_framework_gap(allow_abort):
    tree = ast.parse((SOURCE / "src/plugin_runtime/host/hook_dispatcher.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HookDispatcher")
    cls.body = [
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_apply_error_policy"
    ]
    tree.body = [
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        cls,
    ]
    ns = {"logger": logging.getLogger("test")}
    exec(compile(ast.fix_missing_locations(tree), "actual-hook-dispatcher", "exec"), ns)
    result = SimpleNamespace(aborted=False, hook_name="test", stopped_by=None)
    target = SimpleNamespace(
        entry=SimpleNamespace(error_policy="abort", full_name="caeruvis.latex.render_and_guard")
    )
    ns["HookDispatcher"]()._apply_error_policy(
        target, SimpleNamespace(allow_abort=allow_abort), result, "timeout"
    )
    assert result.aborted == allow_abort


def test_real_host_image_codec_emits_png_and_no_formula_text():
    ns = functions(
        SOURCE / "src/common/data_models/message_component_data_model.py",
        {"BaseMessageComponentModel", "ByteComponent", "TextComponent", "ImageComponent"},
        {
            "ABC": ABC,
            "abstractmethod": abstractmethod,
            "deepcopy": deepcopy,
            "hashlib": hashlib,
            "base64": base64,
            "Seg": SimpleNamespace,  # 平台传输对象替身；执行 Host 原始图片编解码函数。
        },
    )
    tree = ast.parse((SOURCE / "src/plugin_runtime/host/message_utils.py").read_text())
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PluginMessageUtils"
    )
    cls.body = [
        n
        for n in cls.body
        if isinstance(n, ast.FunctionDef)
        and n.name in {"_component_from_dict", "_build_binary_component"}
    ]
    tree.body = [
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        cls,
    ]
    exec(compile(ast.fix_missing_locations(tree), "actual-host-codec", "exec"), ns)
    binary = (ROOT / "docs/examples/integral.png").read_bytes()
    image = ns["PluginMessageUtils"]._component_from_dict(
        {"type": "image", "data": "", "binary_data_base64": base64.b64encode(binary).decode()}
    )
    seg = asyncio.run(image.to_seg())
    assert seg.type == "image" and base64.b64decode(seg.data) == binary
    assert image.content == ""
    assert image.binary_hash == hashlib.sha256(binary).hexdigest()


def test_real_host_hybrid_normalizer_preserves_image_bytes():
    path = SOURCE / "src/plugin_runtime/capabilities/core.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    cls = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "RuntimeCoreCapabilityMixin"
    )
    cls.body = [
        n
        for n in cls.body
        if isinstance(n, ast.FunctionDef)
        and n.name in {"_normalize_plugin_segment", "_normalize_plugin_segments"}
    ]
    tree.body = [
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        cls,
    ]
    ns = {}
    exec(compile(ast.fix_missing_locations(tree), str(path), "exec"), ns)
    binary = base64.b64encode((ROOT / "docs/examples/integral.png").read_bytes()).decode()
    result = ns["RuntimeCoreCapabilityMixin"]._normalize_plugin_segments(
        [{"type": "image", "content": binary}]
    )
    assert result[0]["binary_data_base64"] == binary
    assert result[0]["data"] == ""
