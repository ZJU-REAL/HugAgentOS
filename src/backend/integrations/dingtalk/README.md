# dingtalk 集成

钉钉连接由 service 管理登录、刷新和注销，parsing 解析 CLI 输出及二维码。models/repository 保存连接状态，凭据仍存放在用户独立目录。sandbox 注册环境、挂载及 Cube 注入；临时沙箱缓存按 provider 与 sandbox ID 隔离，30 秒导出缓存和原失效校验不变。

FULL 与 CE 均安装，模型注册独立于运行开关。API/CLI 由宿主显式注册凭据贡献；不会因导入通用 sandbox 自动配置具体集成。挂载顺序保留 DingTalk、Lark、Email、Yida，命令注入顺序保留 DingTalk、Email、Lark、Yida。调用方直接引用所属模块，无旧路径转发。
