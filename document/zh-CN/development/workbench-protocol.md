# 工作台协议

工作台协议在 `packages/workbench-protocol` 内独立构建。它提供 JSON Schema、
TypeScript 类型、校验函数及正反例。当前包保持私有，不发布到 npm。

在包目录运行 `npm ci`、`npm test`、`npm pack`。独立项目可安装生成的 tarball，
从 `@hugagent/workbench-protocol` 导入 `validateApplication`、
`validateContributions` 和 `ApplicationDeclaration`。构建后的校验器不需要宿主源码、
React、CDN 或额外服务。宿主前端在自己的构建前自动构建协议包。

插件的 `extensions["org.hugagent"].ui.version` 使用 3。已有 UI v2 包含资源桥接，
不会改写其语义；旧 v1/v2 工具结果、画布和模块仍走原有解析路径。
在 `ui.contributes` 中增加 `applications`，并把引用模块的 `surface` 声明为 `workspace`：

```json
{
  "version": 3,
  "contributes": {
    "modules": [{"id": "main", "entry": "web/main.html", "surface": "workspace"}],
    "applications": [{
      "schemaVersion": "1.0.0", "id": "reports", "title": "报告",
      "target": {"kind": "plugin", "moduleId": "main"},
      "compatibility": {"protocolVersion": "1.0.0", "requiredCapabilities": ["workspace"]}
    }]
  }
}
```

宿主校验四种 target：builtin、plugin、external 和 command。内置组件与命令必须来自
宿主白名单；插件应用只能引用自己唯一的 workspace 模块。重复 ID、无效模块引用、
越界路径、危险 URL 或不兼容的 required 能力会明确拒绝。错误包含字段路径和固定原因，
不回显被拒绝的内容。损坏的应用不影响健康应用及旧工具结果。

应用可声明 navigation、最多一层 views、permissions、availability、lifecycle、
compatibility 以及 icon.light/dark。模块与图标使用包内 web/ 路径。
Schema 版本精确为 1.0.0；协议能力协商接受 1.0 的补丁版本，
拒绝不同主版本或较新的次版本。未知可选能力可以忽略。声明能力不代表授予权限。

前后端同跑包内 fixtures；Python 使用同一 Schema 的已校验制品，不运行 Node 服务。
Schema 修改后在仓库根目录执行 `node scripts/sync-workbench-schema.mjs`。
宿主构建会检查制品是否一致。Docker 前端构建以仓库根为上下文，
读取前端和协议包；桌面前端构建沿用同一个 npm 构建入口。

本协议交付声明与校验。应用目录、打开页面、SDK 和实例资源由后续功能实现；
单独声明 workspace 模块不会启动页面或授予原生能力。
