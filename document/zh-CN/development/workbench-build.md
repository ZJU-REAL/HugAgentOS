# 工作台构建与离线制品

工作台随宿主前端交付。协议与 SDK 保持独立私有包，当前协议和 SDK 版本为 `1.0.0`。插件 UI v3 用于工作区应用；旧 UI v1/v2 继续保留。两个包均为 `private: true`、`UNLICENSED`，本流程不发布公共 npm。

## 构建顺序

在仓库根目录安装前端锁定依赖后，执行：

```bash
npm --prefix src/frontend ci
npm --prefix src/frontend run build
node scripts/build-workbench.mjs --verify-dist src/frontend/dist
```

前端 prebuild 先校验包清单、锁文件、许可和协议版本，再用前端的锁定 TypeScript/esbuild 构建协议、SDK，并检查后端 Schema 副本。无需在每个共享包重复安装工具。postbuild 输出 `dist/workbench-build.json` 和 `dist/licenses/`。清单只包含相对制品路径、版本及 SHA-256，不含凭据或开发机绝对路径。

缺源文件、缺许可、锁漂移、版本不一致、缺编译制品或旧制品均使构建失败。错误前缀包括 `workbench_missing_file`、`workbench_lock_drift`、`workbench_version_mismatch`、`workbench_stale_artifact` 和 `workbench_distribution_drift`。修正源文件和锁后重新构建，不能跳过门禁交付旧 dist。

## Docker 和 CE

前端 Dockerfile 的 context 是仓库根目录，使用 `docker build -f src/frontend/Dockerfile .`。专用 ignore 文件仅放行前端、协议、SDK、构建脚本和后端 Schema。镜像内按相同顺序构建，共享包许可随最终网页保留。

CE 继续排除组织能力和内部文档，保留基础工作台及两个包。CE 使用 `ce/overlay/src/frontend/package-lock.json`，匹配裁剪后的依赖，Docker 与桌面统一执行 `npm ci`。更新前端依赖时，须在完整 CE 派生树中以 `npm install --package-lock-only --ignore-scripts` 更新该锁，再同步回 overlay。不要复制 EE 锁替代 CE 锁；两版保留原有 edition 门控。

## 独立离线包

```bash
node scripts/pack-workbench.mjs ./artifacts/workbench
```

此命令先构建共享包，再在指定目录生成两个 npm tgz 和 `workbench-artifacts.json`。该清单记录版本、包 SHA-256、制品哈希及 UI 兼容版本。离线消费者使用本地 tgz，不依赖宿主工作树或 CDN。打包无需联网下载依赖，但构建机须已有前端锁定工具。

## 桌面

现有 Tauri 和 UOS 构建入口继续使用标准桌面准备流程。完整包分别构建桌面网页及 CE 登录网页，校验两份清单，再删除构建期依赖。创建 `server-ce.zip` 前再次校验共享包、网页许可和前端锁哈希；缺失或内容变化立即失败。终端用户不运行 Node 或 npm 构建步骤。

原生构建、签名及安装验收按 [桌面说明](../../../desktop/README.md) 执行。网页构建或归档检查通过不能替代 Windows、macOS、UOS 的实际安装及离线验收。
