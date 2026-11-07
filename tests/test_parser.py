import pytest

from latex_engine import parser
from latex_engine.parser import parse, unwrap


@pytest.mark.parametrize(
    "source,kinds,contents",
    [
        ("今天继续学习。", ["text"], ["今天继续学习。"]),
        (
            r"质能关系为 $E=mc^2$。",
            ["text", "math", "text"],
            ["质能关系为 ", "E=mc^2", "。"],
        ),
        (r"$$\frac{a+b}{c}$$", ["math"], [r"\frac{a+b}{c}"]),
        (
            r"已知 \(a+b=c\)，因此 \(c-a=b\)。",
            ["text", "math", "text", "math", "text"],
            ["已知 ", "a+b=c", "，因此 ", "c-a=b", "。"],
        ),
        (
            r"\[\begin{aligned}a&=b+c\\d&=e-f\end{aligned}\]",
            ["math"],
            [r"\begin{aligned}a&=b+c\\d&=e-f\end{aligned}"],
        ),
        ("价格为 $5，预算为 $10。", ["text"], ["价格为 $5，预算为 $10。"]),
        ("The price is $5 or US$10.", ["text"], ["The price is $5 or US$10."]),
        ('The fee is $5. Run `echo "$HOME"`.', ["text"], ['The fee is $5. Run `echo "$HOME"`.']),
        ("价格为 $5，公式 $-x$。", ["text", "math", "text"], ["价格为 $5，公式 ", "-x", "。"]),
        (r"转义 \$5，公式 $5$", ["text", "math"], [r"转义 \$5，公式 ", "5"]),
        ("$5 + x$", ["math"], ["5 + x"]),
        ("$2 x$", ["math"], ["2 x"]),
        (r"前文 $2 \cdot x", ["text", "error"], ["前文 ", ""]),
        ("```latex\n\\frac{1}{2}\n```\n", ["math"], [r"\frac{1}{2}"]),
        ("~~~TeX\n$$x^2$$\n~~~", ["math"], ["x^2"]),
        ("```python\nx = '$foo$'\n```", ["text"], ["```python\nx = '$foo$'\n```"]),
        ("`echo $HOME` 和 $x$", ["text", "math"], ["`echo $HOME` 和 ", "x"]),
        ("前文 $x", ["text", "error"], ["前文 ", ""]),
        (r"前文 \[\frac{x}{y}", ["text", "error"], ["前文 ", ""]),
        ("```tex\nx^2", ["error"], [""]),
        (r"解释 \frac{a}{b} 后文", ["text", "error"], ["解释 ", ""]),
        ("$$x % $$ ignored\n+y$$", ["math"], ["x % $$ ignored\n+y"]),
        (r"$\text{\$5}$", ["math"], [r"\text{\$5}"]),
        (r"$a$ 与 $$b$$", ["math", "text", "math"], ["a", " 与 ", "b"]),
        (r"\\(普通括号)", ["text"], [r"\\(普通括号)"]),
    ],
)
def test_parse(source, kinds, contents):
    spans = parse(source)
    assert [s.kind for s in spans] == kinds
    assert [s.content for s in spans] == contents


def test_unwrap_preserves_multiple_formulas():
    assert unwrap("$a$ + $b$") == "$a$ + $b$"
    assert unwrap(r"\[x^2\]") == "x^2"


def test_unclosed_does_not_expose_suffix():
    spans = parse("前文 $$secret formula\n可能仍属公式")
    assert "secret" not in repr(spans)


def test_long_input_fails_without_partial_source():
    assert parse("a" * 32001)[0].kind == "error"


def test_long_backslash_run_is_not_repeatedly_scanned(monkeypatch):
    backslashes = "\\" * 31_990
    original = parser.escaped
    checks = 0

    def count_escape_checks(text, at):
        nonlocal checks
        checks += 1
        assert checks <= 8
        return original(text, at)

    monkeypatch.setattr(parser, "escaped", count_escape_checks)
    assert parse(backslashes + r"\(x\)") == [
        parser.Span("text", backslashes),
        parser.Span("math", "x"),
    ]


def test_currency_lookahead_skips_plain_suffix():
    class CountedText(str):
        reads = 0

        def __getitem__(self, index):
            self.reads += 1
            assert self.reads < 8 * len(self)
            return super().__getitem__(index)

    text = CountedText("$1 % " * 3200 + "\n" + "a" * 15999)
    assert parse(text) == [parser.Span("text", str(text))]


def test_complete_reply_can_include_chunk_boundaries():
    chunks = ["说明 ", "\\", "[\\fr", "ac{a}{b}", "\\", "] 结束"]
    spans = parse("".join(chunks))
    assert [s.kind for s in spans] == ["text", "math", "text"]


def test_unmarked_multiline_environment_cannot_leak_tail():
    spans = parse("前文 \\begin{aligned}\na &= b \\\\\nc &= d\n\\end{aligned}")
    assert [(s.kind, s.content) for s in spans] == [("text", "前文 "), ("error", "")]


def test_unknown_bare_command_with_braces_is_hidden():
    assert [s.kind for s in parse(r"\Unrecognized{secret}")] == ["error"]
