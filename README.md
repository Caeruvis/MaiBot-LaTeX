## MaiBot LaTeX 公式渲染插件

**MaiBot LaTeX** 使用 **TeX Live / XeLaTeX**，把含公式的整段回复连同说明文字渲染成 **一张 PNG**。不含公式的回复照常发送。目标宿主为 **MaiBot 1.3.19**，插件 SDK 为 **2.21.0**。

## 使用方式

| 入口 | 输入 | 输出 |
| --- | --- | --- |
| 自动回复 | 普通文字与 `$…$`、`$$…$$`、`\(…\)`、`\[…\]` 或 `latex` 围栏 | 将整段回复排成一张图，通过原有发送流程送出 |
| 模型工具 `render_latex_reply` | `source` 填整段回答，`format` 可选 `mixed` 或 `latex` | 生成 PNG，模型通过正常回复原样输出工具返回的 `image_token` |
| 手动命令 `/latex`、`/tex`、`/公式` | 一个公式、支持的环境或单页 LaTeX 文档 | 在命令所在会话发送一张 PNG |

例如，自动回复 `设 $x^2=4$，解为 $x=\pm2$。` 会将说明文字和两个公式放在同一张图里。工具的默认模式 `mixed` 使用同样的输入格式。若使用 `latex` 模式，可以传入带 `\documentclass` 的单页文档，也可以传入带公式分隔符的正文、`align`、`tabular` 或 `tikzpicture` 环境。

插件模板加载 `amsmath`、`amssymb`、`mathtools`、`siunitx`、`booktabs`、`tikz`、`xcolor` 和 `xeCJK`，并用 Fandol 字体排版中文。完整文档可以加载镜像里已安装的其他宏包。混合模式会把普通文字中的 LaTeX 特殊字符转义，Markdown 标记按文字显示。未标记的裸公式仍有识别歧义。工具的 `latex` 模式可以显式传入相应语法。

## 安装

1. 在运行 **MaiBot Runner** 的环境准备可调用的本地 Docker。解压插件后，将 `caeruvis_latex` 放进实例的 `plugins` 目录。在插件目录中构建渲染镜像，首次构建需下载基础镜像与 TeX Live 软件包。

   ```sh
   docker build -t maibot-latex-texlive:local texlive
   ```

2. 在 `_manifest.json` 中填写实际的 `author.url` 和 `urls.repository`，确认插件能通过宿主校验。确认宿主和 SDK 版本，停止机器人并备份 `src/maisaka/builtin_tool/reply.py` 与 `src/services/send_service.py`。
3. 在 **Host 源码根目录** 检查并应用配套补丁。它让回复与发送流程在缺少有效保护回执时停止，避免失败时漏发公式源码。原文件哈希记录在 [host-source-hashes.json](docs/host-source-hashes.json)。

   ```sh
   patch --dry-run -p1 -i /绝对路径/caeruvis_latex/docs/host-1.3.19-fail-closed.patch
   patch -p1 -i /绝对路径/caeruvis_latex/docs/host-1.3.19-fail-closed.patch
   ```

4. 启动并启用插件。先试 `/latex \frac{1}{2}`，再让模型生成带有说明文字和多个公式的回答。在启用插件工具的会话中，模型可发现 `render_latex_reply`。

若宿主曾应用过包含链接例外的旧补丁，请用同版本的原始文件恢复 `src/plugin_runtime/runner/manifest_validator.py`。停用插件前先停止机器人并用 `patch -R` 撤销保护补丁。保护插件缺席时，宿主补丁会停止通过发送服务的出站消息。

## 运行限制

- 编译容器无网络、无宿主目录挂载，以非 root 用户运行。根文件系统只读，临时工作目录、CPU、内存、进程数和执行时间均受限。XeLaTeX 禁用 shell escape。请在可信的本地 Docker 环境中运行。
- 自动回复和工具均只发送单页图片。内容过长、排版溢出、编译失败或本地镜像缺失时，使用简短失败提示。图片缓存和会话标记仅保存在内存。
- `plugin.auto_render=false` 时，含公式的普通回复使用失败提示。模型工具和手动命令仍可显式渲染。其他配置见 [config.example.toml](config.example.toml)。
- 附件和引用保留原有发送路径。工具图片通过同一会话中的标记进入发送流程。

## 本地验证

```sh
MAIBOT_SOURCE=/绝对路径/MaiBot-1.3.19 python -m pytest -q
python tools/render_smoke.py
python tools/package.py
```

`render_smoke.py` 需要已启动的 Docker 和已构建的本地镜像。宿主接口核对见 [INTERFACES.md](docs/INTERFACES.md)。
