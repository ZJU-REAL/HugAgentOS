# 托管应用

管理独立应用数据库、应用项目、集合、版本、发布、MCP 与内部数据接口，以及插件应用资源的准入和清理。HTTP 路由由中央路由表保持原顺序及权限策略；数据库配置通过 config 提供，独立初始化命令为 `python -m features.hosted_apps.hosting_setup`。

应用数据表仍使用独立 store.metadata，不并入平台 Base。模块通过同步 schema 生命周期回调初始化受管理的本机 SQLite 应用库，执行位置保持在站点存储退役之后、平台 schema 调和之前。API、CLI 和容器入口均注册此回调，注册幂等；初始化异常仍阻断启动。云端数据库必须显式配置及预置，不自动创建。桥接模式的跳过条件保持不变。

公开入口为 data、projects、publication、deployments、store、schema 等实际模块。允许 hosted_apps → sites：应用发布关联站点。当前桌面转发仍引用原始桌面桥接实现，属于 routes.json 虚拟归属识别的历史依赖，P5 须通过公开适配接口消除反向循环。已删除废弃 application_site_conversion，不保留兼容转发。

依赖 features.desktop 提供本机认证及能力文件锁，桌面不反向依赖托管应用。执行位置由宿主策略接口提供。
