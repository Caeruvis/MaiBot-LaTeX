"""用本地 TeX Live 容器验证公式、整段回复和 TikZ 渲染。"""

import argparse
import asyncio
import base64
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from latex_engine.config import RenderConfig
from latex_engine.engine import Engine
from latex_engine.render import Renderer


async def run(args):
    renderer = Renderer(RenderConfig(docker_image=args.image, timeout_seconds=10))
    engine = Engine(renderer)
    args.output.mkdir(parents=True, exist_ok=True)

    for name, source, format in (
        ("reply", r"由 $x^2=4$ 可知 $x=pm 2$，速度为 $\qty{2}{\meter\per\second}$。", "mixed"),
        ("align", r"\begin{align*}x^2 &= 4\\ x &= \pm 2\end{align*}", "latex"),
        ("tikz", r"\begin{tikzpicture}\draw (0,0) circle (1cm);\end{tikzpicture}", "latex"),
    ):
        token = await engine.tool(source, format, "smoke")
        if not token:
            raise SystemExit(f"{name} 渲染失败，请检查 Docker 服务和本地镜像。")
        segments = await engine.transform([{"type": "text", "data": token}], "smoke")
        path = args.output / f"{name}.png"
        path.write_bytes(base64.b64decode(segments[0]["binary_data_base64"], validate=True))
        print(path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default=RenderConfig().docker_image)
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/render")
    asyncio.run(run(parser.parse_args()))
