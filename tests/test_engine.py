import asyncio
from types import SimpleNamespace

from latex_engine.engine import Engine
from latex_engine.parser import FAILURE


class FakeRenderer:
    config = SimpleNamespace(max_chars=6000, max_formulas=12)

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def render(self, source, display=True):
        self.calls.append(source)
        await asyncio.sleep(0.005 if source == "a" else 0)
        return None if self.fail else "png:" + source

    def prune(self):
        pass

    def clear(self):
        pass


def test_order_is_preserved_with_concurrent_rendering():
    async def run():
        engine = Engine(FakeRenderer())
        text, _ = engine.protect("前 $a$ 中 $b$ 后", "chat1")
        assert "$" not in text and "a" not in text.replace("MLTX", "").split("⟦")[0]
        out = await engine.transform([{"type": "text", "data": text}], "chat1")
        assert [s["type"] for s in out] == ["text", "image", "text", "image", "text"]
        assert [s.get("binary_data_base64") for s in out if s["type"] == "image"] == [
            "png:a",
            "png:b",
        ]

    asyncio.run(run())


def test_failure_keeps_prose_and_never_source():
    async def run():
        engine = Engine(FakeRenderer(fail=True))
        text, _ = engine.protect(r"前 $\unknown{secret}$ 后", "chat")
        out = await engine.transform([{"type": "text", "data": text}], "chat")
        assert "".join(s["data"] for s in out) == "前 " + FAILURE + " 后"

    asyncio.run(run())


def test_tokens_cannot_cross_sessions_or_survive_expiry():
    async def run():
        engine = Engine(FakeRenderer())
        text, _ = engine.protect("$private$", "a")
        assert (await engine.transform([{"type": "text", "data": text}], "b"))[0]["data"] == FAILURE
        engine.pending.clear()
        assert (await engine.transform([{"type": "text", "data": text}], "a"))[0]["data"] == FAILURE

    asyncio.run(run())


def test_adjacent_text_segments_are_joined():
    async def run():
        out = await Engine(FakeRenderer()).transform(
            [
                {"type": "text", "data": "前 $x"},
                {"type": "text", "data": "^2$ 后"},
                {"type": "at", "data": {"target_user_id": "123"}},
            ],
            "a",
        )
        assert out[1]["binary_data_base64"] == "png:x^2"
        assert out[-1]["type"] == "at"

    asyncio.run(run())


def test_auto_disabled_manual_still_renders():
    async def run():
        engine = Engine(FakeRenderer(), automatic=False)
        assert engine.protect("说明 $x$", "a")[0] == "说明 " + FAILURE
        assert (await engine.command(r"\frac{1}{2}"))[0]["type"] == "image"

    asyncio.run(run())


def test_reentry_is_idempotent_and_has_no_send_side_effect():
    async def run():
        engine = Engine(FakeRenderer())
        text, _ = engine.protect("$x$", "a")
        assert engine.protect(text, "a")[0] == text
        out = await engine.transform([{"type": "text", "data": text}], "a")
        assert await engine.transform(out, "a") == out

    asyncio.run(run())


def test_formula_limit_and_pending_bound():
    async def run():
        engine = Engine(FakeRenderer())
        out = await engine.transform([{"type": "text", "data": "$x$ " * 20}], "a")
        assert sum(s["type"] == "image" for s in out) == 12
        assert sum(s.get("data") == FAILURE for s in out) == 8
        for _ in range(520):
            engine.protect("$x$", "a")
        assert len(engine.pending) == 512

    asyncio.run(run())
