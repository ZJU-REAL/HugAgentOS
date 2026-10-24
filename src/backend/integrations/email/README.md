# email 集成

邮箱绑定、配置生成与连接探测由 service 管理，models/repository 保存账号状态及加密字段。storage/mounts/cube_credentials 保留每用户配置文件路径与注入缓存。注销仅清除文件，保留挂载目录 inode。

FULL 和 CE 均安装；模型加载独立于运行开关，API/CLI 宿主显式注册沙箱贡献。原路由仍由总集成 API 提供，迁移不改变协议、账号归属或运行开关。调用方直接引用所属服务，不保留旧路径转发。
