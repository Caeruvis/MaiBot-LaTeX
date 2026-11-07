"""有序扫描公式、转义符和 Markdown 代码；未闭合公式只产生失败段。"""

import re
from dataclasses import dataclass

FAILURE = "（公式暂时无法显示）"
MAX_TEXT = 32000
TOKEN_RE = re.compile(r"⟦MLTX:[0-9a-f]{32}⟧")
FENCE_RE = re.compile(r"(?m)^[ \t]{0,3}(`{3,}|~{3,})([^\n]*)\n?")
BACKTICK_RE = re.compile(r"`+")
INLINE_END_RE = re.compile(r"(?m)^[ \t]{0,3}(?:`{3,}|~{3,})|\n[ \t\r]*\n|(`+)")
AMOUNT_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
AMOUNT_BOUNDARY = " \t\r\n.,，。;；:：!！?？)）元美欧英日港-–"
TEX_AFTER_NUMBER_RE = re.compile(r"\s*\\[A-Za-z]")
OPERATOR_AFTER_NUMBER_RE = re.compile(r"\s*[+\-*/=<>^_]")
BARE_RE = re.compile(
    r"\\(?:frac|dfrac|tfrac|sqrt|sum|prod|int|iint|iiint|oint|lim|"
    r"begin|end|alpha|beta|gamma|theta|lambda|sigma|omega|partial|nabla|"
    r"mathbf|mathbb|mathrm|left|right|overline|underline|vec|hat)(?![A-Za-z])"
    r"|\\[A-Za-z]+(?=\{)"
)


@dataclass(frozen=True)
class Span:
    kind: str
    content: str
    display: bool = False


def escaped(text: str, at: int) -> bool:
    count = 0
    at -= 1
    while at >= 0 and text[at] == "\\":
        count += 1
        at -= 1
    return count % 2 == 1


def _closing(text: str, start: int, token: str) -> int:
    """TeX 注释中的分隔符不会闭合公式；单美元与双美元分别处理。"""
    pos = text.find(token, start)
    while pos >= 0:
        comment = text.find("%", start, pos)
        if comment >= 0:
            start = comment + 1
            if not escaped(text, comment):
                newline = text.find("\n", start)
                if newline < 0:
                    return -1
                start = newline + 1
        else:
            start = pos + 1
            if token != "$" or not (
                text.startswith("$$", pos) or (pos > 0 and text[pos - 1] == "$")
            ):
                if not escaped(text, pos):
                    return pos
        if start > pos:
            pos = text.find(token, start)
    return -1


def _inline_code_end(text: str, start: int) -> int:
    end = BACKTICK_RE.match(text, start).end()
    for match in INLINE_END_RE.finditer(text, end):
        if match[1] is None:
            break  # 围栏和新段落不能闭合行内代码。
        if len(match[1]) == end - start:
            return match.end()
    return end


def _currency(text: str, pos: int) -> bool:
    """保留普通金额，优先保护带闭合符或明显 TeX 命令的公式。"""
    amount = AMOUNT_RE.match(text, pos + 1)
    if not amount:
        return False
    end = amount.end()
    if end == len(text):
        return True
    if text[end] not in AMOUNT_BOUNDARY:
        return False
    if TEX_AFTER_NUMBER_RE.match(text, end):
        return False
    close = _closing(text, end, "$")
    if close < 0:
        return True
    if OPERATOR_AFTER_NUMBER_RE.match(text, end, close):
        return False
    tick = text.find("`", end, close)
    while tick >= 0:
        end = tick + 1 if escaped(text, tick) else _inline_code_end(text, tick)
        if end > close:
            return True
        tick = text.find("`", end, close)
    # 后一个美元符若引出金额，或另有闭合符的公式，保留当前金额。
    if AMOUNT_RE.match(text, close + 1):
        return True
    return (
        close + 1 < len(text)
        and text[close - 1] in AMOUNT_BOUNDARY
        and not text[close + 1].isspace()
        and text[close + 1] not in ".,，。;；:：!！?？)）"
        and _closing(text, close + 1, "$") >= 0
    )


def unwrap(source: str) -> str:
    source = source.strip()
    for opening, closing in (("$$", "$$"), (r"\[", r"\]"), (r"\(", r"\)"), ("$", "$")):
        if source.startswith(opening):
            at = _closing(source, len(opening), closing)
            if at == len(source) - len(closing):
                return source[len(opening) : at].strip()
    return source


def parse(text: str, *, detect_bare: bool = True) -> list[Span]:
    if len(text) > MAX_TEXT:
        return [Span("error", "")]
    result: list[Span] = []
    pending: list[str] = []

    def emit(kind: str, content: str, display: bool = False) -> None:
        if pending:
            result.append(Span("text", "".join(pending)))
            pending.clear()
        result.append(Span(kind, content, display))

    i = 0
    while i < len(text):
        if text.startswith("\\\\", i):
            # 成对消耗反斜杠，避免为同一串反斜杠反复向后扫描。
            pending.append(text[i : i + 2])
            i += 2
            continue
        token = TOKEN_RE.match(text, i)
        if token:
            emit("token", token.group())
            i = token.end()
            continue
        fence = FENCE_RE.match(text, i)
        if fence:
            marker, info = fence.groups()
            closing = re.compile(
                r"(?m)^[ \t]{0,3}"
                + re.escape(marker[0])
                + "{"
                + str(len(marker))
                + r",}[ \t]*(?:\n|$)"
            ).search(text, fence.end())
            end = closing.end() if closing else len(text)
            words = info.strip().split(maxsplit=1)
            language = words[0].lower() if words else ""
            if language in {"tex", "latex"}:
                content = text[fence.end() : closing.start()] if closing else ""
                emit(
                    "math" if closing and content.strip() else "error",
                    unwrap(content),
                    True,
                )
            else:
                pending.append(text[i:end])
            i = end
            continue
        if text[i] == "`" and not escaped(text, i):
            end = _inline_code_end(text, i)
            pending.append(text[i:end])
            i = end
            continue
        opening = closing = ""
        display = False
        if not escaped(text, i):
            if text.startswith(r"\[", i):
                opening, closing, display = r"\[", r"\]", True
            elif text.startswith(r"\(", i):
                opening, closing = r"\(", r"\)"
            elif text.startswith("$$", i):
                opening = closing = "$$"
                display = True
            elif text[i] == "$" and not _currency(text, i):
                opening = closing = "$"
        if opening:
            close = _closing(text, i + len(opening), closing)
            if close < 0:
                emit("error", "", display)
                break  # 不能证明尾部是普通文字，保守隐藏至消息结束。
            content = text[i + len(opening) : close]
            emit("math" if content.strip() else "error", content, display)
            i = close + len(closing)
            continue
        if detect_bare and not escaped(text, i) and BARE_RE.match(text, i):
            emit("error", "")
            i = len(text)  # 未标记公式没有可靠的结束边界，隐藏剩余尾部。
            continue
        pending.append(text[i])
        i += 1
    if pending:
        result.append(Span("text", "".join(pending)))
    return result
