"""插件配置；不提供失败时回传源码的选项。"""

from maibot_sdk import Field, PluginConfigBase


class GeneralConfig(PluginConfigBase):
    config_version: str = "0.1.0"
    auto_render: bool = Field(
        default=True, description="将含公式的整段回复渲染为一张图；关闭时显示安全提示"
    )


class RenderConfig(PluginConfigBase):
    docker_image: str = Field(
        default="maibot-latex-texlive:local",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._/@:-]*$",
        description="已构建的本地 TeX Live 渲染镜像",
    )
    font_size: int = Field(default=28, ge=12, le=64, description="公式字号（像素）")
    foreground: str = Field(default="#18202b", pattern=r"^#[0-9a-fA-F]{6}$", description="公式颜色")
    background: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$", description="背景颜色")
    padding: int = Field(default=16, ge=8, le=64, description="四周留白")
    scale: float = Field(default=2.0, ge=1.0, le=3.0, description="图片像素倍率")
    max_width: int = Field(
        default=1600,
        ge=320,
        le=2400,
        description="最大排版宽度，超出时返回失败提示",
    )
    max_height: int = Field(default=1200, ge=100, le=2400, description="最大排版高度")
    max_chars: int = Field(default=6000, ge=100, le=12000, description="单个公式最多字符")
    max_formulas: int = Field(default=12, ge=1, le=32, description="每条消息最多渲染公式数")
    concurrency: int = Field(default=2, ge=1, le=4, description="同时渲染的公式数")
    timeout_seconds: float = Field(default=8.0, ge=1.0, le=10.0, description="含排队的单张图片超时")
    cache_entries: int = Field(default=128, ge=0, le=512, description="内存图片缓存上限")
    cache_seconds: int = Field(default=900, ge=0, le=3600, description="内存缓存有效秒数")


class LatexConfig(PluginConfigBase):
    plugin: GeneralConfig = Field(default_factory=GeneralConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)
