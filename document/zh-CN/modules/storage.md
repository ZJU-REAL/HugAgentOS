# 对象存储

> 最后更新：2026-06-11

HugAgentOS 把所有持久化文件——用户上传、AI 生成产物（artifact）、知识库文档、导出报表——统一收敛到一个**存储后端抽象**之上。通过 `STORAGE_TYPE` 环境变量在三种实现之间切换：本地文件系统（社区版默认）、S3 兼容对象存储、阿里云 OSS（云存储属商业版 EE，能力位 `cloud_storage`）。业务代码只面向协议编程，更换后端零侵入。

## 协议与工厂

存储协议定义在 `src/backend/core/storage/protocol.py::StorageBackend`（ABC），共 7 个方法：

| 方法 | 说明 |
|---|---|
| `upload(file_path, storage_key)` | 上传本地文件，返回存储 URL |
| `upload_bytes(content, storage_key)` | 上传字节流 |
| `download(storage_key, local_path)` | 下载到本地路径 |
| `download_bytes(storage_key)` | 下载为字节 |
| `generate_presigned_url(storage_key, expires_in=900)` | 生成预签名直链 |
| `delete(storage_key)` | 删除 |
| `exists(storage_key)` | 存在性检查 |

工厂在 `core/storage/factory.py`：`get_storage()` 返回进程级懒加载单例；`get_storage_backend()` 按 `STORAGE_TYPE` 分发。云后端的 import 收在分支内——CE 派生树物理不含 `s3.py` / `oss.py` 时，工厂在 `local` 配置下依然可用。

```python
from core.storage import get_storage, generate_storage_key

storage = get_storage()
key = generate_storage_key(env="dev", user_id=uid, category="uploads", filename=name)
url = storage.upload_bytes(data, key)
```

### 存储 key 规范

`generate_storage_key()` 生成标准化 key：

```
{env}/{category}/{user_id}[/{chat_id}]/{timestamp}_{filename}
```

`category` 由资源类型映射（`get_storage_category_for_resource()`）：`artifact→artifacts`、`kb_document→kb_documents`、`upload→uploads`、`export→exports`、`temp→temp`。文件名经 `secure_filename` 清洗，本地后端另有路径穿越防护（越界 key 落入 `sanitized/` 哈希目录，见 `local.py::_get_full_path`）。

## 三种后端实现

### local（社区版默认）

`core/storage/local.py::LocalStorageBackend`——写入 `STORAGE_PATH`（默认 `./storage`，容器内通常挂载为 `/app/storage`）。`generate_presigned_url` 返回本地路径（无真正签名语义），适合开发与单机部署。

```bash
STORAGE_TYPE=local
STORAGE_PATH=./storage
```

### s3（商业版 EE）

`core/storage/s3.py::S3StorageBackend`——基于 boto3，兼容 AWS S3 及 MinIO 等 S3 协议服务（signature v4，3 次重试）。

```bash
STORAGE_TYPE=s3
S3_BUCKET=your-bucket            # 必填
S3_ENDPOINT=https://s3.amazonaws.com   # 可选，S3 兼容服务填自己的 endpoint
S3_REGION=us-east-1
S3_ACCESS_KEY=...
S3_SECRET_KEY=...
S3_CDN_DOMAIN=cdn.example.com    # 可选，CDN 加速域名
S3_PRESIGNED_URL_EXPIRY=900      # 预签名有效期（秒）
```

### oss（商业版 EE）

`core/storage/oss.py::OSSStorageBackend`——基于 oss2 SDK 的阿里云 OSS 实现，支持统一 key 前缀。

```bash
STORAGE_TYPE=oss
OSS_ENDPOINT=https://oss-cn-hangzhou.aliyuncs.com   # 必填
OSS_BUCKET=your-bucket                              # 必填
OSS_ACCESS_KEY_ID=...                               # 必填
OSS_ACCESS_KEY_SECRET=...                           # 必填
OSS_KEY_PREFIX=hugagent/        # 可选，bucket 内统一前缀
OSS_PRESIGNED_URL_EXPIRY=900
```

## Artifact 存储（AI 生成产物）

MCP 工具产出的文件（报表、图表、文档等）走 `src/backend/core/artifacts/store.py`，是协议层之上的一个轻量产物仓：

- **双模式**：`STORAGE_TYPE=local` 时字节写 `{STORAGE_PATH:-result}/artifacts/`；`oss` 时上传 OSS、本地只留索引条目；
- **JSON 索引**：`{base}/artifacts/index.json` 维护 `file_id → 元数据` 映射；OSS 模式下索引还会备份到 OSS（key `artifacts/_index.json`），容器重启后自动恢复；
- **SVG 自动适配**：保存 SVG 时自动扩展 viewBox（`core/content/svg_fit.py`），防止模型产出的图被裁切；
- 注意：该索引仓的云分支目前只识别 `oss`，`STORAGE_TYPE=s3` 时 artifact 产物按本地模式落盘（通用存储协议层不受影响）。

## 文件上传 / 下载链路

```
上传                                      下载
────                                      ────
POST /v1/file/upload                      GET /files/{file_id}
  · 50MB 上限                               · mode=direct|presigned
  · key: {env}/{uid}/user_uploads/           · inline=true 内联展示
         {artifact_id}/{filename}            · 鉴权 + 审计（file.download）
  · storage.upload_bytes()                 GET /files/{file_id}/preview
  · 写 artifacts 表（DB）                    · Office → PDF 在线预览
  · 返回 download_url=/files/{file_id}
```

- **上传**：`src/backend/api/routes/v1/file_upload.py`——持久化到对象存储并写 `artifacts` 表（ORM `core/db/models/artifact.py::Artifact`，含 `storage_key` / `user_folder_id` / `team_id` 等归属字段），支持指定个人文件夹（我的空间）；
- **下载**：`src/backend/api/routes/files.py`——`/files/{file_id}` 是**非 v1 前缀**的历史稳定路径（产物 URL 兼容性），支持 direct / presigned 两种模式、inline 内联、归属鉴权与审计落表；
- 知识库文档上传走独立路由（100MB 上限），见 [知识库](./knowledge-base.md)。

## myspace_cache 本地缓存与沙箱回写

「我的空间」文件与代码沙箱之间有一层后端镜像缓存（详见 [沙箱模块](./sandbox.md)）：

- 缓存目录：`{STORAGE_PATH}/myspace_cache/{user_id}/...`（`core/sandbox/_common.py::myspace_cache_dir`），团队文件另有 `team_cache_dir(team_id)` 共享缓存；
- **seed**：持久沙箱首次创建会话时，把缓存目录文件灌入沙箱 `/workspace/myspace/{user_id}/`，后续按 mtime 增量同步；
- **懒加载**：沙箱内 Read/Glob/Grep 命中缺失文件时，按路径解析 artifact，从对象存储按需下载并物化进沙箱（`core/llm/tools/myspace_vfs.py::materialize_into_sandbox`）;
- **反向同步**：Write/Edit/Delete/Move 等工具把沙箱侧改动写回——同时更新 `artifacts` 表（对象存储）与 myspace_cache 镜像，保证下次 seed 一致。

这意味着对象存储是**唯一真源**，myspace_cache 只是加速镜像，可随时清空重建。

## 最佳实践

| 场景 | 推荐配置 |
|---|---|
| 本地开发 / 单机体验 | `STORAGE_TYPE=local`（默认），把 `STORAGE_PATH` 挂为持久卷 |
| 生产（商业版 EE） | `STORAGE_TYPE=oss` 或 `s3`，开启预签名直链下载减轻后端带宽 |
| 多副本部署 | 必须用 OSS/S3——local 后端不跨实例共享 |
| 离线生产 | local + 宿主机卷挂载（`HOST_STORAGE_PATH`，见 [离线生产部署](../deployment/offline-production.md)） |

## 相关源码

| 路径 | 职责 |
|---|---|
| `src/backend/core/storage/protocol.py` | `StorageBackend` 抽象协议 |
| `src/backend/core/storage/factory.py` | 工厂、单例、storage key 规范 |
| `src/backend/core/storage/local.py` | 本地文件系统后端（含路径穿越防护） |
| `src/backend/core/storage/s3.py` | S3 兼容后端（商业版 EE） |
| `src/backend/core/storage/oss.py` | 阿里云 OSS 后端（商业版 EE） |
| `src/backend/core/artifacts/store.py` | AI 产物仓（JSON 索引 + local/OSS 双模式） |
| `src/backend/api/routes/v1/file_upload.py` | `/v1/file/upload` 上传链路 |
| `src/backend/api/routes/files.py` | `/files/{file_id}` 下载 / 预览 |
| `src/backend/core/db/models/artifact.py` | `Artifact` ORM（storage_key 与归属字段） |
| `src/backend/core/sandbox/_common.py` | `myspace_cache_dir` / `team_cache_dir` |
| `src/backend/core/llm/tools/myspace_vfs.py` | 我的空间 ↔ 沙箱 双向同步层 |

相关文档：[沙箱](./sandbox.md) · [项目空间与我的空间](./projects-myspace.md) · [环境变量参考](../deployment/environment-variables.md) · [版本对比](../editions/overview.md)
