# 字体目录（社区版占位）

商业版打包的方正系列字体（方正小标宋/仿宋/楷体/黑体简体）为**商业授权字体**，
不随社区版分发。本目录保留是为了让各 Dockerfile 的
`COPY resources/fonts /tmp/fonts` 正常工作。

需要中文报表/图表字体时，把任意开源 CJK 字体（如思源宋体 Source Han Serif、
思源黑体 Source Han Sans，SIL OFL 协议）的 `.ttf` 放进本目录后重新构建镜像，
或在运行环境设置 `JX_FONT_DIR` 指向已安装字体目录。缺省时图表自动回退系统
CJK 字体（WenQuanYi / Noto Sans CJK）；文档导出的字体名仅作样式声明，
阅读端无该字体时由 Office 自行替换显示。
