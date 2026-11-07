## 核实范围

**当前目标已经由用户确认：MaiBot 1.3.19 + SDK 2.21.0。** 本地源代码版本由 `pyproject.toml` 核实；目录为 `/Users/yangzheren/Documents/ChatGPT/dsh-video-storyboard-maibot-plugin-adapt/work/host`，SDK 代码目录为同级 `maibot-plugin-sdk-main`。这些目录没有可核实的 Git 提交记录，因此使用 [逐文件 SHA-256](source-evidence.json) 固定本次检查依据。另行发现的 `Lufris/.venv` 是旧版本，未作为插件接口依据。

**实时运行配置、实际生效的 QQ 适配器及该平台的消息投递尚未验证。** 本次实现、模拟协议测试和真实浏览器测试均在独立工作目录内进行，没有调用在线模型、发送 QQ 消息或修改运行中的机器人。

## 已确认事实

| 接口 | 源码位置 | 已核实行为 |
|---|---|---|
| `create_plugin()` / `MaiBotPlugin` | `src/plugin_runtime/runner/plugin_loader.py:593`；SDK `maibot_sdk/plugin.py` | 入口以包方式加载，支持相对导入；必须实现加载、卸载、配置更新方法 |
| `maisaka.reply.before_post_process` | `src/maisaka/builtin_tool/reply.py:81`；`src/maisaka/chat_loop_service.py:571` | 接收完整最终正文；返回 response、skip_post_process 等设置；HookSpec 禁止 abort |
| 回复拆分和清洗 | `src/maisaka/builtin_tool/context.py:377` | skip_post_process 为 true 时跳过文本拆分、错字注入和文本清洗 |
| `send_service.before_send` | `src/services/send_service.py:112`、`:872` | 在调用 Platform IO 之前改写消息，允许 abort，调用方检查 aborted 并停止发送 |
| 异常策略 | `src/plugin_runtime/host/hook_dispatcher.py:407`、`:599` | 异常和超时进入错误策略；禁止 abort 的钩子会把 abort 策略按 skip 处理；熔断可跳过执行 |
| 消息序列协议 | `src/plugin_runtime/host/message_utils.py:87`、`:243`、`:415` | 文本为 type=text/data；图片可带 binary_data_base64；SessionMessage 使用 raw_message 列表 |
| 命令发送 | SDK `maibot_sdk/capabilities/send.py`；Host `src/plugin_runtime/capabilities/core.py:569` | send.hybrid 经 Host 规范化后调用发送服务；return_details 可取得 sent/message_id |
| 命令拦截 | `src/chat/message_receive/bot.py:280`、`:350` | 命令先于普通聊天处理；返回三元组第三项是消息拦截等级，非零停止后续处理 |
| 浏览器渲染 | SDK `maibot_sdk/capabilities/render.py`；Host `src/services/html_render_service.py:751` | render.html2png 支持 selector、viewport、倍像素、超时及 allow_network=False |
| 本地声明校验 | `src/plugin_runtime/runner/manifest_validator.py:205`、`:250`、`:624` | 原版要求作者主页、仓库为非空 HTTP(S) URL，版本号为三段式 |

行号对应补丁应用前的本地源码。源文件内容及版本的摘要记录在 JSON 中，补丁另附原文件哈希。

官方说明：[插件 Hook](https://docs.maibot.chat/plugin/hooks)、[命令](https://docs.maibot.chat/plugin/commands)、[回复扩展](https://docs.maibot.chat/plugin/reply-extensions)。在线文档会更新；实现以本次本地版本源码为准。命令文档把返回第三项称为权重，目标源码实际用作 intercept_message_level，本插件遵循源码。

## 实现选择与理由

**完整回复保护 → 跳过清洗/拆分 → 发送前渲染替换 → 原宿主发送。** 前置钩子为 late，先让同阶段普通处理器完成，再把公式替换为随机、绑定会话的临时标记；公式明文只保留在插件内存中。发送钩子把标记换成图片，并处理该消息内尚未处理的文本公式。相邻文本段先合并，公式和其他消息段的相对顺序不变。

本插件没有注册模型工具或 ReplyExtension。官方 ReplyExtension 仅在模型选中对应 plugin_options 时执行，无法单独提供“自动处理所有回复”的保证。手动入口使用所有用户可触发的 Command。（依据：SDK 装饰器和 Host reply 扩展代码。）

渲染后端选择 **KaTeX 0.16.22** ，与 Host 浏览器能力组合生成 PNG。资源及字体离线内嵌，输入通过 JSON 数据传入；throwOnError 开启，strict 和 trust 回调直接拒绝不支持的行为，避免错误源码以红字渲染。独立调用使用独立宏对象；宏展开次数受限。超尺寸按失败处理；数学尺寸不通过 maxSize 静默裁减。渲染脚本失败返回一像素空图，Python 将其识别为失败并改为安全提示。（来源：[KaTeX 官方选项说明](https://katex.org/docs/options.html)、[支持函数](https://katex.org/docs/supported.html)。）

图片内容不包含 LaTeX 注释、调试信息或源码附件。缓存仅内存存储，插件自身不打印公式和渲染异常；Host 自己的模型请求历史和诊断日志可能保留模型原回复，本任务的保证边界是聊天平台出站内容。

## 宿主补丁为何必要

**纯插件无法在这个版本上对插件失联、熔断和前置钩子失效作绝对保证。** 已确认：前置 HookSpec 的 allow_abort=False；reply.py 的异常分支继续用原文；发送消息反序列化失败时原版会继续旧消息。任何声称“只加 error_policy=abort 就全覆盖”的实现都会遗漏这些路径。

随包 [补丁](host-1.3.19-fail-closed.patch) 增加两道回执校验：完整回复处理之后、最终发送之前。回执包含经过保护的内容哈希；缺少回执、记录了异常、正文被后续插件改动时停止当前操作。最终消息反序列化失败也停止发送。回执是正常插件协作中的一致性检查，不是抵御恶意插件伪造的密码学身份认证。

插件声明由宿主原有规则校验。安装前须填写 `author.url` 和 `urls.repository`。

补丁的操作影响是：插件停用或不可用会停止发送服务中的所有出站消息，包括普通文字；已有部分消息成功时，不会由本插件重试整个批次。需要移除插件时，应先撤销补丁。当前源代码副本只用于生成和验证补丁，实际 Host 尚未应用修改。

## 兼容边界

- 覆盖核实的 Maisaka 完整回复链路、经过 send_service 的文本消息及直接命令。绕过该发送服务的第三方网关直发不受保护；需要单独接入并验证。
- 顶层文本中的公式可识别，普通代码按代码保留；未标记数学存在不可消除的歧义。附件、合并转发内部、卡片和自定义载荷不自动递归解析。
- 在前置钩子之前就将公式删除或拆断的其他插件不属于已验证范围。当前本机智能分段插件只预先缓存切分结果；含公式原文被标记替换后其哈希不匹配，因此不会再切分这条回复。
- 公式图片和文字是原序列内的有序组件；同一会话不同轮次的自动回复顺序仍由宿主调度。手动命令另有每会话串行锁。跨会话无共用原文缓冲区。
- 真正逐片段向外发送的流式接口未接入；只有完整回复聚合后的片段边界测试。
- 其他宿主版本、浏览器字体环境、实际 QQ/NapCat/SnowLuma 投递需重新验收，不扩大兼容声明。
