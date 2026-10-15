# 容器响应式布局

聊天工具栏跟随所在面板的可用宽度调整，首页、对话页、项目页和分屏共用同一套布局。

- 工具区与模型／发送区分组，空间不足时自然换行。
- 标签逐级收起，保留图标、可访问名称和模式退出按钮；模型名称保留并省略长文本。
- 触屏使用真实的 44px 点击区域，参与排版，不叠加透明命中层。
- 项目、能力目录和知识库卡片按可用宽度自动排列，长名称不会撑破网格。
- 系统安全区、页面高度与弹出菜单仍按其实际视口处理。

实现入口：前端的 components/chat/ComposerToolbar.tsx、styles/chat/toolbar-layout.css、
styles/projects.css 和 styles/catalog/。容器边界负责提供宽度，后代负责响应；
不要让元素查询自身，也不要通过 JavaScript 重复维护 CSS 断点。

在 WSL 项目目录进入 src/frontend，使用项目支持的 Node.js 版本执行：

    npm run test:container-layout-browser
    npm run test:canvas-layout-browser
    npm run test:chat-width-browser
    npm run lint
    npm run build

新增用例覆盖固定大窗口中的窄容器、手机窗口、分数像素边界、深浅主题、
长名称、全部模式、触屏目标、键盘退出及发送／中止。
