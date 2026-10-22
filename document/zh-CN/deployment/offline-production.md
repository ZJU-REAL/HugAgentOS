# 离线生产部署（商业版 EE）

> 最后更新：2026-10-16 ｜ [English](../../en/deployment/offline-production.md) ｜ 返回 [部署指南](README.md)

离线交付使用 `deploy` 分支的编译加固镜像及配套部署 kit。
构建和打包在供应方的独立 `deploy` 工作区执行；目标机加载镜像并使用同批 kit 启动。
构建、分支同步、上传和生产发布分别遵守项目授权规则。

## 联网侧：准备完整交付包

1. 核对已验证的 `deploy` 提交、干净工作区、产品身份及目标 CPU 架构。
2. 完整读取该提交的 `deploy_kit/save_pack_ee.sh` 和配套 Compose 文件。
3. 构建并打包：

   ```bash
   # 在 deploy 分支的工作区根目录执行
   bash deploy_kit/save_pack_ee.sh --build
   # 编译镜像已存在且已核对本次提交时，可直接打包
   bash deploy_kit/save_pack_ee.sh
   ```

脚本校验编译后端、MCP、前端及必备基础设施镜像，并按本机存在情况加入可选镜像。
交付方还须核对目标 profiles 的全部依赖，不能把“可选镜像未安装”当作目标机不需要它。
镜像名、版本、输出目录与文件名以该提交的脚本和 manifest 为准，不手工改名。
交付包同时包含镜像归档、manifest 和匹配的部署 kit；License 单独安全交付。
不得用普通源码镜像或只包含后端和前端的增量包替代完整 EE 交付包。

## 目标侧：加载与上线

按交付 kit 的说明配置独立项目名、数据目录、License 和必要的受保护环境文件。
升级前备份数据库与文件，确认镜像 manifest、目标架构和所选 profiles 一致。
授权后加载指定镜像归档，并使用同批 kit 的启动入口及完整 Compose 覆盖文件。
目标机不能联网拉镜像时，必须先确认所需镜像已全部加载，启动不得触发现场构建。
不使用旧源码部署脚本，也不自动选取目录里时间最新但未经核验的归档。

验收至少包括 `/health`、`/ready`、登录、附件、模型调用及实际启用的沙箱能力。
数据库迁移会改变 schema；数据卷持久化不代表升级不会修改业务数据。
失败时按交付记录中的镜像、配置和数据备份恢复，不删除数据卷。

## 提示词快照随包带入

系统提示词存在数据库 `content_blocks` 表（`prompt_versions` 版本池 + `prompt_hub` 提示词广场），**不进仓库、不在镜像里**。要把新提示词推上生产，需作为单独数据文件随镜像包带入并在生产侧导入：

### 1. 联网侧导出

通过 `GET /v1/content/prompts/export` 导出快照。请求使用源环境的管理员或系统配置令牌。
将响应的 `data` 对象保存为 `prompts_snapshot_<ts>.json`，不要保存完整响应信封。

### 2. 拷贝

把 `prompts_snapshot_<ts>.json` 与镜像 tarball 一起拷到生产机。

### 3. 生产侧导入

前提：生产 backend 镜像须已包含 `POST /v1/content/prompts/import` 接口（`src/backend/api/routes/v1/content.py`）。在新 backend 起好后：

```bash
BACKEND_CONTAINER=your-backend-container
docker cp prompts_snapshot_<ts>.json "${BACKEND_CONTAINER}:/tmp/"
docker exec "$BACKEND_CONTAINER" curl -sS -X POST \
  'http://localhost:3001/v1/content/prompts/import?overwrite=true' \
  -H "Authorization: Bearer <生产ADMIN_TOKEN>" \
  -H 'Content-Type: application/json' \
  -d @/tmp/prompts_snapshot_<ts>.json
```

API 导入会自动失效提示词缓存，**无需重启 backend**。

> 这是按需的一次性数据操作：DB 卷持久化，不必每次发版都导提示词——只在要把新提示词推上生产时执行。

## 相关源码

| 功能 | 文件 |
|---|---|
| EE 完整镜像包（deploy 分支） | `deploy_kit/save_pack_ee.sh` |
| EE 编译镜像（deploy 分支） | `docker/Dockerfile.backend-compiled`、`docker/Dockerfile.mcp-compiled` |
| Compose 覆盖（deploy 分支） | `docker-compose.prod.yml`、`docker-compose.deploy.yml`；运行时以交付 kit 为准 |
| 提示词导出 / 导入 | `src/backend/api/routes/v1/content.py` |
| 提示词版本池 | `src/backend/core/llm/prompt_versions.py` |
