# 对象存储

CE 和 EE 的云端 Docker 部署默认使用 S3 兼容存储。桌面离线本机模式和无 Docker 单机安装保留本地文件。

设置 STORAGE_TYPE=s3，并配置 S3_ENDPOINT、S3_BUCKET、S3_REGION、S3_ACCESS_KEY 和 S3_SECRET_KEY。后端和沙盒控制器均须能够访问该服务地址。

可选的 docker/object-storage/compose.object-storage.yml 启动独立 S3 服务。它使用独立数据卷，不发布宿主端口，也不共用 Milvus 存储。

在新的 CE checkout 中运行 python3 scripts/configure-ce-docker.py。初始化工具生成独立随机访问密钥和秘密密钥，并拒绝覆盖已有 .env。

先构建后端镜像，再启动桶初始化容器。对象挂载由独立沙盒挂载服务管理，无需在宿主机准备共享传播目录，然后启动服务。

已有部署切换存储前，先备份配置、数据库和文件引用。复制所需对象并逐对象校验 SHA-256。保留源对象。

存储工厂位于 core/files/storage/factory.py。S3StorageBackend 使用 boto3 并提供目录操作。LocalStorageBackend 服务桌面本地文件。OSS 和 OBS 适配仍属于 EE。

生成物仓库支持 S3、OSS 和 OBS。元数据记录保存在本地，并镜像到对象存储。迁移时保留原存储键，以保持历史下载引用有效。

我的空间直接读取授权目录。云端沙盒挂载相同对象前缀，不再依赖后台镜像或文件登记监听器。

目录移动包含多个对象操作，不是原子操作。文件导入、挂载恢复和权限验证见 space-files 部署指南。

文件列表中的搜索按文件名匹配当前文件夹的全部直接子项（不区分大小写），不受已加载页数影响。服务端先筛选再分页；清空关键词后恢复普通目录列表。

大目录搜索超过扫描时限时会明确报错，不将未完成的扫描显示为无结果。

[Space files](../deployment/space-files.md)
