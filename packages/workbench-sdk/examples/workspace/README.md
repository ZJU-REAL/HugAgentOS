# Independent workspace example

Copy this directory outside the host repository.
Install the administrator-supplied SDK tarball with --offline --ignore-scripts.
esbuild 0.27.3 and its native platform binary must already be in your approved npm cache.
Run npm run build, npm run validate and npm run preview.
Use the preview's Open button. An unopened example creates no iframe.
The permission dropdown offers allowed, denied and missing-capability scenarios.

The real page uses workspace.navigate, data.query, file.pick and file.save.
It passes source IDs and file references, never credentials or complete files.
Replace the example.org data endpoint with your approved endpoint before a real host exercise.
The preview intentionally simulates data and file results. It is not a real authorization test.
ZIP dist/plugin.json and dist/web/ at the archive root for the host plugin importer.

独立工作区示例：把本目录复制到宿主仓库外。
用 --offline --ignore-scripts 安装管理员提供的 SDK tar 包。
构建前须缓存 esbuild 0.27.3 及对应平台二进制。
运行 npm run build、npm run validate、npm run preview。
点击预览的 Open 按钮才创建 iframe；权限下拉框提供允许、拒绝和缺能力场景。
真实页面使用 workspace.navigate、data.query、file.pick 和 file.save。
消息只传数据源 ID 与文件引用，不传凭据或完整文件。
真实宿主演练前，把 example.org 数据接口替换为已批准接口。
预览的数据与文件结果明确属于模拟，不能作为实际鉴权证据。
导入宿主时，将 dist/plugin.json 和 dist/web/ 放在 ZIP 根目录。
