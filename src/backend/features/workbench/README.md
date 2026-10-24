# 工作台

拥有应用目录、外部应用边界、插件界面声明、安装修订、授权租约、资源归属、后台观察及工作台配置。托管业务应用仍属于 hosted_apps；工作台配置继续存入 ContentBlock，不增加数据库表。

FULL 与 CE 均安装。API 在中央有序路由表注册，工作台内部路由组与配置保护保持原顺序。CE 的 migration_snapshot 位于相同功能路径，通过已有 overlay 保留版型差异。

依赖 core 的认证、通用插件及技能运行、数据存储和扩展接口；工作台通过宿主注册 PackageUpgradePolicy 与 ResourceOwner，core 不保留工作台实现转发。站点元数据依赖 features.sites。工作台依赖桌面安装登记、版本修订和能力绑定；界面声明投影由宿主注入，桌面不反向依赖工作台。

专属测试与 HTTP/进程观察夹具位于 tests/features/workbench。测试使用独立临时库和 .scratch 数据目录，不连接真实账号或用户数据。
