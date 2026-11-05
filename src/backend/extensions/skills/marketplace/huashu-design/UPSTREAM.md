# 花叔 Design 上游记录

- 上游仓库：<https://github.com/alchaincyf/huashu-design>
- 上游作者：花叔（GitHub: `alchaincyf`）
- 固定 commit：`9b01ee8751679724dd43d855a532944aaa2c2b59`
- commit 时间：2026-10-31T02:50:57+09:00
- 导入日期：2026-11-03
- 许可证：MIT，见 `LICENSE`

## 本地适配

- 删除上游仓库维护文件（README、SECURITY、测试提示词、`package-lock.json`、`scripts/tests`、`.gitignore`）。
- **不导入视频与音频管线**：删除背景音乐 `assets/bgm-*.mp3`、音效 `assets/sfx/`、云端能力 `scripts/cloud/`（豆包 TTS、火山方舟视频评审），以及 `add-music.sh`、`mix-voiceover.sh`、`render-narration.sh`、`narrate-pipeline.mjs`、`sfx-cues.sh`、`render-video.js`、`render-video-seek.js`、`verify-video.sh` 和对应的 6 份参考文档。原因：体积（约 27MB 音频）、会把内容发到外部云服务、沙箱没有视频渲染链路。`references/modules/motion.md` 开头和 `SKILL.md` 路由表注明本版本只做 HTML 动画。
- `SKILL.md`：`$HD` 改为 `{baseDir}`；联网核实改用平台的 `internet_search` / `web_fetch`；品味档案的存放位置说明改为当前工作环境；删除「版本检查」一节（安装目录不是 git 克隆，不做自更新）；触发词去掉"导出 MP4/GIF"；注明 `capabilities.py` 输出的 `npx skills add` 安装建议在本平台不适用。
- `package.json` 去掉 `playwright`（沙箱已内置固定版本，重复安装可能冲突），保留 `pdf-lib`、`pptxgenjs`、`sharp`。这三个包需要管理员重建沙箱依赖后才会安装；安装前 PDF / PPTX 导出和缩略图脚本不可用，HTML 设计本身不受影响。
- `export_deck_pdf.mjs`、`export_deck_stage_pdf.mjs`、`export_deck_pptx.mjs`、`gen_deck_thumbs.mjs` 把第三方包的 ESM `import` 改为 `createRequire` 加载：沙箱里的 Node 包在 `NODE_PATH` 上，ESM 解析会忽略它。

除以上适配外，参考资料、风格样张、示例和其余脚本均来自上述固定 commit。
