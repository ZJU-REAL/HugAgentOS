# 站点托管与发布

负责站点模型与仓储、静态内容服务、访问密码与速率限制、站点打包、本机来源发布，以及内置站点插件升级。设计选择工具由 extension.register_tools 注册；模型由 FULL 和 CE 安装清单加载，不依赖运行开关。

HTTP 路由保持原有路径、顺序和权限策略；sites_serve 仍在 API 应用的原位置挂载，其余路由通过中央路由表注册。Site 表结构与约束不变，CE 通过 edition.py 选择权限字段和访问策略。

公开入口包括 service、management、listing、packaging、repository 及各 API 路由。调用方直接依赖实际模块，不保留旧路径转发。依赖内核数据库、文件、项目权限和插件基础设施。

站点通过 extension.register_gateway_contributions 向桌面网关注册上传格式、接收实现和回执本地化；桌面网关不导入站点实现。sites → desktop 是单向依赖，用于桥接账号校验、能力修订和发布传输。HTTP 执行位置策略来自宿主接口，注册在 API/CLI 初始化阶段完成。

版本差异统一由 edition.py 选择 ORM 字段、访问策略及请求字段；CE 只覆盖此入口。
