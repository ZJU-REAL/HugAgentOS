# Ordinary external webpage

Deploy index.html on an approved HTTPS origin.
Replace the example.org URL in ../workspace/external-application.json with that exact URL.
Validate the declaration, then configure it through the host administrator interface.
It opens in a separate browser page. It needs no SDK or iframe bridge.
The sample changes only its UI filter; it has no simulated authentication endpoint.
Your external service owns its login and session. Never read host cookies or accept a general host token.
Target-limited exchange requires your server's approved audience and exact callback integration.
The local permission preview does not validate target-system authentication.

普通外部网页：将 index.html 部署到已批准的 HTTPS 来源。
把 ../workspace/external-application.json 的 example.org 替换为该准确地址，校验后由管理员配置。
页面独立打开，无需 SDK 或 iframe 桥接。
示例仅改变界面筛选项，不提供模拟认证后端。
外部服务自行管理登录和会话，不读取宿主 Cookie，也不接受宿主通用 token。
目标限定换票需要服务端已批准的受众和精确回调；本地权限预览不能验证真实登录。
