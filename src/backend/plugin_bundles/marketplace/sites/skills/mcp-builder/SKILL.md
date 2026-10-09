---
name: mcp-builder
version: 1.8.1
description: 通过对话创建、发布、编辑和更新数据库查询 MCP，并接入当前用户的个人 MCP。适用于用户要求将数据提供为 MCP 工具或迭代已有 MCP 服务；服务名称、数据来源和工具定义由实际需求决定。
---

# 对话创建和发布 MCP

用户给出数据来源、需要查询的内容和可暴露字段后，完成创建、发布和个人接入。
当前能力托管声明式数据库查询工具，不执行任意用户服务器代码。

## 创建

1. 根据用户需求确认服务名称、授权数据来源、查询范围和可暴露字段。已有文件先读取。
   来源不可用时报告缺口，不编造记录来冒充真实来源。
2. 调用 `manage_application(action="list")`。用户要更新已有服务时，用标题与应用编号确认目标。
   多个候选不明确时询问。更新时复用 app_id，不创建重复应用。
3. 新建调用 `manage_application(action="create", payload={"title":"<用户确定的服务名称>","kind":"mcp"})`。
   只有用户需要关联已有网页时传 site_id；独立 MCP 不要求网页。
4. 按数据定义 SQL 表，再导入记录。所有操作把 app_id 放在顶层，不能放进 payload。
   字段类型为 text、integer、number、boolean、date、json。访客读写默认关闭。
   用 insert 操作写入授权来源的记录，导入后核对返回表名和记录数。
5. 调用 `manage_application(action="query", app_id="<编号>", payload={"table":"<表名>"})`，
   检查需要的字段和记录。数据为空时如实说明，不能宣称已经导入。

## 发布并添加到个人 MCP

调用独立的 `publish_mcp` 工具。以下占位符必须替换为实际应用与工具定义：

```json
{
  "app_id": "<创建或查询返回的应用编号>",
  "tools": [{
    "name": "<用户需求确定的合法工具名称>",
    "description": "<工具实际用途和查询范围>",
    "table": "<实际表名>",
    "fields": ["<实际允许返回的字段>"],
    "filters": ["<实际查询字段>"]
  }]
}
```

- 只提供已存在且用户允许暴露的字段。json 不作为过滤参数；limit 和 offset 为保留参数。
- 当前账号由平台注入。不要填写别人的账号、任意地址、Authorization 或访问凭据。
- 工具自动发布、验证连接并加入当前账号的个人 MCP；访问凭据在服务器加密保存。
- 检查回执：只有 installed=true 且 connection_verified=true 才报告“已发布并添加到我的 MCP”。
  说明服务名称和版本。提醒用户下一轮对话可使用该工具；不要保证本轮工具列表已动态刷新。
- 权限不足时，说明管理员需开放“自助添加 MCP”。不要改用其他入口绕过权限。
- 部分失败时按回执区分“发布成功”和“个人接入失败”，修复后用原 app_id 重试。
- 重复发布更新同一条个人 MCP。手动对外客户端配置通过站点管理获取凭据；不要在对话里输出秘密。
- 撤销：`manage_application(action="revoke_mcp", app_id="<编号>")`，服务和个人接入同时停用。
- 桌面本机或混合模式通过站点插件转发到已登录云端账号；账号不可用时重新登录，不回退创建另一套本机服务。

## 项目编辑与迭代

- 创建 MCP 时 kind=mcp，后台生成独立源码项目。旧服务首次发布或从卡片编辑时补建项目。
- 先调用 `manage_application(action="source", app_id="<原编号>")`，取得 project_id、source_path、
  当前定义、revision 和真实数据表。卡片编辑会进入这个云端项目，桌面混合模式也使用云端对话。
- 项目根目录 `mcp.json` 是工具定义，包含 app_id、version、tools。读取后修改 tools，保留 app_id
  和 version，不能把访问凭据或业务记录写入该文件。新增数据表先调用 table 操作。
- 项目编辑时用 `manage_application(action="publish_project", app_id="<原编号>")` 发布已保存的定义。
  后端校验字段和版本，更新同一服务地址及个人 MCP，并将新版本同步回项目。只改文件不会立即改变线上服务。
- 版本冲突时先 source 查询当前发布状态，并读取项目文件，核对已发布配置后保留用户修改，
  再更新基准版本。不得不核对就强行重试。source 返回 definition 是项目草稿，published_definition 是线上配置。
- project_synced=false 表示服务发布成功但项目文件同步失败；明确报告并核对版本，不能宣称所有步骤已完成。
- 初次发布仍可调用 publish_mcp（明确的 tools 参数），它也生成项目并同步定义。更新时始终复用原 app_id。
