# 模块 · 动画 / 视频 / 宣传片

> **HugAgentOS 版本说明（优先于本模块其余内容）**：本版本只保留 **HTML 动画**（浏览器里播放的动画、产品演示页、可交互的动态信息图）。
> 导出 MP4/GIF、HyperFrames 渲染、配乐（BGM）、音效（SFX）、解说配音和成片 AI 评审相关的脚本与参考文档都没有随技能分发。
> 下文提到 `voiceover-pipeline.md`、`video-export.md`、`hyperframes-backend.md`、`audio-design-rules.md`、`sfx-library.md`、`ai-video-review.md` 或 `render-video*.js`、`add-music.sh` 等时，一律跳过；
> 「声音」和「导出」两步改为：交付可在浏览器打开的 HTML 动画，并告诉用户本平台暂不支持导出视频和配乐。用户确实需要视频时，建议用浏览器录屏或专业剪辑工具。

时间轴驱动的一切走这里：HTML 动画、产品功能演示、launch film、带解说的概念视频、导出 MP4/GIF。这是本 skill 能力最多的模块，所以它有自己的路由。先过 SKILL.md 的窄桥（三方向门照走，初稿形态是方向板），再按这份手册做。

## 先判断是哪种片子

| 片子 | 主线 | 必读 |
|---|---|---|
| 短动画 / 概念演示（<1 分钟，无解说） | 下面「通用流程」 | `storyboard-basics.md` → `animation-pitfalls.md` → 后端文件 |
| 产品 UI 是主角（功能演示、商单、界面展示） | 通用流程＋UI 展示 | `ui-demo-animation.md`（截图运镜 vs HTML 重建决策树、UI 展示八式）＋`assets/cursor.jsx` |
| launch film / 品牌宣传片（「Apple 级」预期） | 三方向方向板 → 选定后写万字导演稿 | `launch-film-director-notes.md`、`cinematic-patterns.md` |
| 带解说的长视频（≥1 分钟、讲清一个概念） | 解说驱动：先写解说稿再做画面 | `voiceover-pipeline.md`＋`assets/narration_stage.jsx` |
| 素材很多且同质（20+ 截图/作品，要表达规模） | 画廊式场景 | `hero-animation-case-study.md`、`apple-gallery-showcase.md` |
| 想看多个版本 / 多平台分发 | 多视角并行 | `multi-perspective-parallel-case-study.md` |

## 通用流程

1. **三方向方向板**：每方向 1 张方向板——hero 关键帧的真实 HTML 静帧截图 ×1–2＋色板条＋一句气质定位＋参照作品名。不是三支成片，但必须是渲出来的画面（`references/directions.md`）。三个方向的差异落在运动语言和材料上，不只是颜色。
2. **分镜卡**：任何动画开工前先出轻量分镜卡，每一镜先是一张会动的封面（`storyboard-basics.md` §5 的格式，含 [CAMERA] 列和验收帧号）。≥20s 的片子分镜卡写进 `导演稿.md`，并写**视觉密度条款**——只写叙事和运镜的导演稿会产出线框稿（`animation-best-practices.md` §6.5）。
3. **镜头语言**：zoom / pan / 转场等镜头级运动先读 `camera-language.md`（预算制、镜间语法），实现看 `gsap-recipes.md` §9。
4. **选后端**（下表），写动画。动手前必读 `animation-pitfalls.md`。
5. **看帧**：关键时刻截帧，真的去看——裁切、碰撞、空帧、只在纸面上成立的节奏。每次换场后的第一秒也要截。
6. **声音**：默认交付带 SFX＋BGM 的 MP4（下文「声音」）。
7. **导出与校验**（下文「导出」）。

## 后端

| 后端 | 什么时候用 | 读 |
|---|---|---|
| **HyperFrames**（新动画默认） | 要渲染成片、要确定性、要 60fps | `hyperframes-backend.md`（选型边界、合成契约、老 demo 迁移）＋`gsap-recipes.md` |
| **Stage + Sprite**（`assets/animations.jsx`） | 单文件交付、纯交互演示、弱 runtime | `animations.md` |
| 解说驱动（`assets/narration_stage.jsx`） | 带配音的长视频 | `voiceover-pipeline.md` |

手写 Stage / Sprite（不用 `animations.jsx`）时必须实现两件事，否则录视频必出问题：tick 第一帧同步设 `window.__ready = true`；检测到 `window.__recording === true` 时强制 `loop = false`。

## 两个最常见的失败

- **带配音的 PowerPoint**：每个 scene 各自独立 layout＋文字 fade-up＋整页 opacity 切换。整片应该是一个连续的运动叙事：选 1–2 个 hero element 跨 scene 持续存在，每段是它的状态变化，scene 之间 morph 不切（`voiceover-pipeline.md`「铁律」）。
- **画面里画伪 chrome**：底部进度条、时间码、版权署名条和 Stage 自带的 scrubber 撞车。画面只放叙事内容（`animation-pitfalls.md` §11）。

## 声音（默认带）

无声版本是半成品。BGM＋SFX 双轨同时做，只做 BGM 是三分之一的完成度。

- SFX 按 `audio-design-rules.md` 设计 cue 清单，用 `assets/sfx/<category>/*.mp3`（37 个预制，`sfx-library.md`）；发布 hero 约 6 个/10s，工具演示 0–2 个/10s；`scripts/sfx-cues.sh` 打点
- BGM：`scripts/add-music.sh`（6 首场景化配乐）
- 交付前 `ffprobe -select_streams a` 确认有音轨
- 用户说「不要音频」「纯画面」「我自己配音」才跳过

## 导出

- HyperFrames：`npm run check`（五门审计，暗色电影风加 `--no-contrast`）→ `npx hyperframes render --fps 60` → `scripts/verify-video.sh`
- 自研管线：`scripts/render-video.js`（25fps 中间产物）或 `scripts/render-video-seek.js --fps=60`（逐帧 seek，真 60fps、无黑帧，走 Stage 时钟时用）→ `scripts/convert-formats.sh` 派生 60fps MP4 和调色板优化 GIF
- 带解说：`bash "$HD/scripts/render-narration.sh" demo.html --timeline=_narration/timeline.json [--bgm-mood=educational]`（内部调 `scripts/mix-voiceover.sh` 混人声；单独混音也可以直接用它）
- 终渲后可选 AI 看片评审（云能力，自备 key、显式确认，见 SECURITY.md）：`uv run "$HD/scripts/cloud/ai-review-video.py" --video <成片> --context 导演稿.md --yes`；无 key 用 `scripts/verify-video.sh` 截帧人工看（`ai-video-review.md`）
- 完整说明：`video-export.md`

**水印**：动画产出默认带「Created by Huashu-Design」（幻灯片、信息图、原型、网页一律不加）；第三方品牌的非官方致敬动画加前缀「非官方出品 · 」；用户说不要就去掉。模板见 `video-export.md` 末节。

**Gate**：≥45s 的长片渲染前，`scripts/design-gate-hook.sh` 会查 `direction-approved.md`，缺文件就拦下渲染；用户明说跳过时用 `SKIP_DESIGN_GATE=1` 放行。

## 起手组件

| 文件 | 用途 |
|---|---|
| `assets/animations.jsx` | Stage＋Sprite＋useTime＋Easing＋interpolate |
| `assets/narration_stage.jsx` | NarrationStage＋Scene＋Cue＋Subtitles（解说驱动） |
| `assets/cursor.jsx` | 产品演示里的光标：四种形状、弧线轨迹、点击涟漪、帧确定 |
| `assets/ios_frame.jsx`、`android_frame.jsx`、`macos_window.jsx`、`browser_window.jsx` | 设备与窗口外壳 |

## 引擎层（预留）

这个模块的方法（导演判断、运动语言、节奏、声音设计）和执行引擎（渲染、出声、校验）是分开的两层。上面列的是现有引擎；以后接入新的引擎时，接在「后端」表和「声音」「导出」两节里，方法层的文件不用跟着改。
