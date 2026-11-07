"""从核实过的 1.3.19 源码生成可审阅补丁；不会修改指定 Host。"""

import argparse
import difflib
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(host: Path, output: Path) -> None:
    changes = {}
    path = "src/maisaka/builtin_tool/reply.py"
    before = (host / path).read_text(encoding="utf-8-sig")
    after = before.replace(
        "    before_post_process_kwargs = before_post_process_result.kwargs\n",
        """    # Caeruvis LaTeX strict guard: 缺少有效处理回执时停止本次回复。
    from src.services.latex_guard import verified

    before_post_process_kwargs = before_post_process_result.kwargs
    protected_value = {
        key: before_post_process_kwargs.get(key)
        for key in ("response", "skip_post_process", "enable_splitter", "enable_chinese_typo")
    }
    if (
        before_post_process_result.errors
        or before_post_process_result.aborted
        or not verified(before_post_process_result.custom_results, "reply", protected_value)
    ):
        return "", post_process_options
""",
        1,
    )
    old = '        logger.warning(f"{tool_ctx.runtime.log_prefix} 回复文本后处理前 Hook 调用失败，将使用默认策略: {exc}")\n'
    new = '        logger.warning("回复公式保护不可用，已停止本次回复")\n        reply_text = ""\n'
    if old not in after or after == before:
        raise SystemExit("Host reply.py 与已验证接口不符，未生成补丁")
    after = after.replace(old, new, 1)
    changes[path] = (before, after)

    path = "src/services/send_service.py"
    before = (host / path).read_text()
    old = '    mutated_message = message\n    raw_message = hook_result.kwargs.get("message")\n'
    new = """    mutated_message = message
    raw_message = hook_result.kwargs.get("message")
    if hook_name == "send_service.before_send":
        from src.services.latex_guard import verified

        if hook_result.errors or not verified(hook_result.custom_results, "send", raw_message):
            hook_result.aborted = True
            return hook_result, message
"""
    if before.count(old) != 1:
        raise SystemExit("Host send_service.py 与已验证接口不符，未生成补丁")
    after = before.replace(old, new, 1)
    old = '            logger.warning(f"Hook {hook_name} 返回的 message 无法反序列化，已忽略: {exc}")\n'
    new = """            if hook_name == "send_service.before_send":
                hook_result.aborted = True
                logger.warning("公式保护后的消息无效，已停止发送")
            else:
                logger.warning("Hook 消息无效，已保留原消息")
"""
    if old not in after:
        raise SystemExit("Host 消息反序列化处理与核实版本不符")
    changes[path] = (before, after.replace(old, new, 1))
    changes["src/services/latex_guard.py"] = (
        "",
        (ROOT / "latex_engine/guard.py").read_text(),
    )
    patch = ""
    provenance = {}
    for path, (before, after) in changes.items():
        patch += "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile="a/" + path if before else "/dev/null",
                tofile="b/" + path,
            )
        )
        if before:
            provenance[path] = hashlib.sha256((host / path).read_bytes()).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(patch)
    (output.parent / "host-source-hashes.json").write_text(json.dumps(provenance, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("host", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/host-1.3.19-fail-closed.patch")
    args = parser.parse_args()
    build(args.host.resolve(), args.output)
