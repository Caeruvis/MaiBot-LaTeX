"""用真实 Chromium 与目标 Host 的页面配置/截图函数验证离线图片。"""

import argparse
import ast
import asyncio
import base64
import functools
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from latex_engine.config import RenderConfig
from latex_engine.render import Renderer


def host_capture_class(host: Path):
    source = (host / "src/services/html_render_service.py").read_text()
    tree = ast.parse(source)
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "HTMLRenderService"
    )
    names = {
        "_configure_page",
        "_capture_image",
        "_handle_network_route",
        "_is_network_request_allowed",
    }
    cls.body = [
        node
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    tree.body = [
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        cls,
    ]
    namespace = {
        "functools": functools,
        "urlparse": urlparse,
        "_NETWORK_ALLOW_SCHEMES": frozenset({"about", "blob", "data", "file"}),
    }
    exec(
        compile(
            ast.fix_missing_locations(tree),
            str(host / "src/services/html_render_service.py"),
            "exec",
        ),
        namespace,
    )
    return namespace["HTMLRenderService"]()


async def run(args):
    from io import BytesIO

    from PIL import Image
    from playwright.async_api import async_playwright

    args.output.mkdir(parents=True, exist_ok=True)
    capture = host_capture_class(args.host)
    report = {
        "backend": "actual Chromium + extracted Host 1.3.19 capture methods",
        "network_requests": [],
        "cases": {},
    }
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=args.browser, headless=True)

        class Capability:
            async def html2png(self, html, **kw):
                context = await browser.new_context(
                    viewport=kw["viewport"],
                    device_scale_factor=kw["device_scale_factor"],
                )
                try:
                    page = await context.new_page()
                    page.on(
                        "request",
                        lambda req: (
                            report["network_requests"].append(req.url)
                            if req.url.startswith(("http:", "https:", "file:"))
                            else None
                        ),
                    )
                    request = SimpleNamespace(
                        html=html,
                        selector=kw["selector"],
                        wait_for_selector=kw["wait_for_selector"],
                        timeout_ms=kw["render_timeout_ms"],
                        allow_network=False,
                        wait_until="load",
                        wait_for_timeout_ms=0,
                        full_page=False,
                        omit_background=False,
                    )
                    await capture._configure_page(page, request)
                    image = await capture._capture_image(page, request)
                    width, height = Image.open(BytesIO(image)).size
                    return {
                        "image_base64": base64.b64encode(image).decode(),
                        "mime_type": "image/png",
                        "width": width,
                        "height": height,
                    }
                finally:
                    await context.close()

        renderer = Renderer(Capability(), RenderConfig(timeout_seconds=3.0), ROOT / "vendor/katex")
        cases = {
            "fraction": (r"\frac{a+b}{c}", True),
            "powers": (r"x_i^2+y_{n+1}^{\alpha}", True),
            "radical": (r"\sqrt{x^2+y^2}", True),
            "integral": (r"\int_0^1 x^2\,\mathrm{d}x=\frac{1}{3}", True),
            "matrix": (r"\begin{pmatrix}a&b\\c&d\end{pmatrix}", True),
            "aligned": (r"\begin{aligned}a&=b+c\\d&=e-f\end{aligned}", True),
            "inline": (r"E=mc^2", True),
            "unknown": (r"\UnsupportedCommand{secret}", False),
            "untrusted": (r"\href{https://example.com}{secret}", False),
            "macro_recursion": (r"\def\a{\a}\a", False),
            "oversize": (r"\rule{2000em}{1em}", False),
            "html_injection": (r"\text{</script><img src=https://example.com>}", True),
            "document": (
                r"\documentclass{article}\begin{document}x\end{document}",
                False,
            ),
        }
        for name, (source, expected) in cases.items():
            image = await renderer.render(source, name != "inline")
            report["cases"][name] = {
                "expected_image": expected,
                "got_image": image is not None,
                "passed": (image is not None) == expected,
            }
            if image:
                path = args.output / (name + ".png")
                path.write_bytes(base64.b64decode(image))
                report["cases"][name]["size"] = Image.open(path).size
            print(name, report["cases"][name]["passed"], flush=True)
        await browser.close()
    # 图片只拼版便于人工核验；测试输入和单张输出也保留。
    images = [
        Image.open(args.output / (name + ".png")).convert("RGB")
        for name in (
            "fraction",
            "powers",
            "radical",
            "integral",
            "matrix",
            "aligned",
            "inline",
        )
        if (args.output / (name + ".png")).exists()
    ]
    if images:
        sheet = Image.new(
            "RGB",
            (
                max(im.width for im in images) + 40,
                sum(im.height for im in images) + 20 * (len(images) + 1),
            ),
            "#e9edf2",
        )
        y = 20
        for im in images:
            sheet.paste(im, (20, y))
            y += im.height + 20
        sheet.save(args.output / "contact-sheet.png")
    report["passed"] = (
        all(case["passed"] for case in report["cases"].values()) and not report["network_requests"]
    )
    (args.output / "render-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("host", type=Path)
    parser.add_argument(
        "--browser",
        default="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/render")
    asyncio.run(run(parser.parse_args()))
