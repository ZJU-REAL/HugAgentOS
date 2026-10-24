# lark 集成

飞书连接按 parsing、process、configuration、login、service 拆分，state 统一持有共享应用初始化任务及用户登录任务。service 与后台配置任务使用同一状态，重复初始化不启动第二个任务。models/repository 保存连接状态；sandbox/storage/mounts/cube_credentials 保持每用户 HOME 和文件凭据流，不改用 tenant token。二维码沿用 integrations.dingtalk.parsing 的纯渲染函数。

FULL 与 CE 均安装，模型注册独立于运行开关。API/CLI 由宿主显式注册凭据贡献；不会因导入通用 sandbox 自动配置具体集成。挂载顺序保留 DingTalk、Lark、Email、Yida，命令注入顺序保留 DingTalk、Email、Lark、Yida。调用方直接引用所属模块，无旧路径转发。
