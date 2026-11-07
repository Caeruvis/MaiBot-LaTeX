import asyncio
import re
from copy import deepcopy
from types import SimpleNamespace

import pytest
from latex_plugin_test import COMMAND_PATTERN, create_plugin
from latex_plugin_test.latex_engine.engine import Engine
from latex_plugin_test.latex_engine.guard import verified

from tests.test_engine import FakeRenderer


class Sender:
    def __init__(self, success=True):
        self.calls = []
        self.success = success

    async def hybrid(self, segments, stream, **kwargs):
        self.calls.append((stream, deepcopy(segments)))
        return {
            "sent": self.success,
            "message_id": "fake-platform-id" if self.success else None,
        }

    async def text(self, text, stream, **kwargs):
        self.calls.append((stream, [{"type": "text", "content": text}]))
        return True


def make_plugin(sender=None, renderer=None):
    plugin = create_plugin()
    plugin.set_plugin_config({})
    plugin._set_context(SimpleNamespace(send=sender or Sender()))
    plugin.engine = Engine(renderer or FakeRenderer())
    return plugin


def message(text):
    return {
        "message_id": "m1",
        "session_id": "s1",
        "platform": "qq",
        "timestamp": "0",
        "message_info": {"user_info": {"user_id": "bot", "user_nickname": "bot"}},
        "raw_message": [{"type": "text", "data": text}],
        "processed_plain_text": text,
    }


def test_real_sdk_registration():
    plugin = make_plugin()
    components = {c["name"]: c for c in plugin.get_components()}
    assert set(components) == {
        "latex",
        "protect_formulas",
        "render_and_guard",
        "render_latex_reply",
    }
    meta = components["render_and_guard"]["metadata"]
    assert (
        meta["error_policy"] == "abort" and meta["mode"] == "blocking" and meta["order"] == "late"
    )
    assert components["latex"]["metadata"].get("permission", "") != "operator"


@pytest.mark.parametrize(
    "command",
    [
        "/latex x^2",
        "/tex \\frac{a}{b}",
        "/公式 \n\\begin{matrix}1\\\\2\\end{matrix}",
        "/latex",
    ],
)
def test_command_patterns(command):
    assert re.fullmatch(COMMAND_PATTERN, command)


def test_command_boundary():
    assert not re.fullmatch(COMMAND_PATTERN, "/latexfoo x")


def test_complete_hook_path_and_receipts():
    async def run():
        plugin = make_plugin()
        early = await plugin.protect_formulas(
            response="说明 $E=mc^2$。",
            session_id="s1",
            skip_post_process=False,
            enable_splitter=True,
            enable_chinese_typo=True,
        )
        args = early["modified_kwargs"]
        assert args["skip_post_process"] and "mc" not in args["response"]
        final = await plugin.render_and_guard(
            message=message(args["response"]),
            typing=False,
            set_reply=True,
            reply_message_id="origin",
        )
        safe = final["modified_kwargs"]["message"]
        assert [s["type"] for s in safe["raw_message"]] == ["image"]
        assert safe["processed_plain_text"] == "[公式图片]"
        assert verified([final["custom_result"]], "send", safe)
        assert final["modified_kwargs"]["reply_message_id"] == "origin"
        assert plugin.ctx.send.calls == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "reply,source",
    [
        (r"计算 $2 \cdot x$。", r"2 \cdot x"),
        ("计算 $5 % internal-note\n+ x$。", "5 % internal-note\n+ x"),
        ("当 $5 % internal-note\n+ x $时", "5 % internal-note\n+ x "),
        ("前言`\n\n```latex\nx^2 % internal-note\n```", "x^2 % internal-note"),
    ],
)
@pytest.mark.parametrize("render_fails", [False, True])
def test_formula_source_does_not_reach_send_text(reply, source, render_fails):
    async def run():
        renderer = FakeRenderer(fail=render_fails)
        plugin = make_plugin(renderer=renderer)
        protected = await plugin.protect_formulas(
            response=reply,
            session_id="s1",
            skip_post_process=False,
            enable_splitter=True,
            enable_chinese_typo=True,
        )
        text = protected["modified_kwargs"]["response"]
        assert source not in text
        final = await plugin.render_and_guard(message=message(text))
        outgoing = final["modified_kwargs"]["message"]
        assert source not in outgoing["processed_plain_text"]
        assert all(source not in seg["data"] for seg in outgoing["raw_message"])
        assert len(renderer.calls) == 1 and source in renderer.calls[0]
        assert outgoing["is_picture"] is not render_fails

    asyncio.run(run())


@pytest.mark.asyncio
async def test_tool_renders_once_and_only_same_session_can_send_image():
    renderer = FakeRenderer()
    plugin = make_plugin(renderer=renderer)
    result = await plugin.render_latex_reply(source="先看 $x^2=4$，然后看 $x=2$。", stream_id="s1")
    token = result["image_token"]
    assert result["success"] and len(renderer.calls) == 1
    assert await plugin.engine.transform([{"type": "text", "data": token}], "s2") == [
        {"type": "text", "data": "（公式暂时无法显示）"}
    ]
    early = await plugin.protect_formulas(
        response=token,
        session_id="s1",
        skip_post_process=False,
        enable_splitter=True,
        enable_chinese_typo=True,
    )
    final = await plugin.render_and_guard(message=message(early["modified_kwargs"]["response"]))
    assert [seg["type"] for seg in final["modified_kwargs"]["message"]["raw_message"]] == ["image"]
    assert len(renderer.calls) == 1 and plugin.ctx.send.calls == []


@pytest.mark.asyncio
async def test_tool_failure_does_not_return_source():
    plugin = make_plugin(renderer=FakeRenderer(fail=True))
    result = await plugin.render_latex_reply(source="$private$", stream_id="s1")
    assert result == {"success": False, "error": "（公式暂时无法显示）"}


def test_plain_messages_preserve_content():
    async def run():
        plugin = make_plugin()
        msg = message("普通回复")
        out = await plugin.render_and_guard(message=msg)
        assert out["modified_kwargs"]["message"] == msg

    asyncio.run(run())


def test_command_open_to_all_and_deduplicates_delivery():
    async def run():
        plugin = make_plugin()
        kwargs = dict(stream_id="s1", message=message("/latex x"), matched_groups={"source": "x"})
        first = await plugin.latex_command(**kwargs)
        second = await plugin.latex_command(**kwargs)
        assert first[2] == second[2] == 2
        assert len(plugin.ctx.send.calls) == 1
        assert plugin.ctx.send.calls[0][1][0]["type"] == "image"

    asyncio.run(run())


def test_command_send_failure_never_retries_image_or_echoes_source():
    async def run():
        sender = Sender(success=False)
        plugin = make_plugin(sender)
        result = await plugin.latex_command(
            stream_id="s1", matched_groups={"source": r"\frac{private}{2}"}
        )
        assert result[0] is False
        assert len(sender.calls) == 2
        assert sender.calls[1][1] == [{"type": "text", "content": "（公式暂时无法显示）"}]

    asyncio.run(run())


def test_same_session_commands_do_not_reorder_and_other_session_is_independent():
    async def run():
        plugin = make_plugin()
        await asyncio.gather(
            plugin.latex_command(stream_id="s1", matched_groups={"source": "a"}),
            plugin.latex_command(stream_id="s1", matched_groups={"source": "b"}),
            plugin.latex_command(stream_id="s2", matched_groups={"source": "c"}),
        )
        assert [segs[0]["content"] for stream, segs in plugin.ctx.send.calls if stream == "s1"] == [
            "png:\n\\[\na\n\\]\n",
            "png:\n\\[\nb\n\\]\n",
        ]
        assert plugin.ctx.send.calls[0][0] == "s2"
        assert not plugin._command_locks

    asyncio.run(run())


def test_config_rejects_css_injection():
    plugin = create_plugin()
    with pytest.raises(ValueError):
        plugin.set_plugin_config({"render": {"foreground": "red;position:fixed"}})
