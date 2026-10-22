# Code Canvas 运行时事实与 EmployeeField 验证

本文件承载 `yida-canvas-custom-page` 的运行时细节与原生组件验证示例，SKILL.md 只保留摘要与链接。

## 运行时事实（以 `vc-deep-yida/src/components/yida-code-canvas` 源码为准）

- `factory.tsx` 读取 `runtimeCode` 和 `importedModules`；用 `<Component {...props} />` 渲染，**只透传** `code / runtimeCode / importedModules / pageType` 四个属性。
- `prototype.tsx` 的物料配置也只声明这四个属性，**没有** `dataSource` / `dataSourceMap` 绑定。
- 代码经 `new Function` 包裹执行，wrapper 只注入 `window`（`iframeWindow` / `parentWindow`），**不注入 `this` 上下文**。因此 `YidaComp` 是普通 React 函数组件，**`this.utils.yida.*`、`this.dataSourceMap`、`export function didMount()` 等普通页面契约都不可用**。
- 代码执行后必须返回 `YidaComp`、`YidaComp.default` 或组件函数。
- Canvas 要读宜搭数据，只能在组件内**自写 HTTP 调用**（fetch 宜搭开放 API / 连接器）或依赖 props 注入，即“自己补一座数据桥”。需要平台数据桥的数据驱动页请回退 `yida-custom-page`。

> 📦 依赖白名单表、windowAlias 映射、编译端点，以及「预发正常、线上 `antd is not defined`」根因与物料侧修复方向已拆到 [dependencies-and-cdn.md](dependencies-and-cdn.md)。

## 宜搭原生组件判断

`EmployeeField`、`SelectField`、`DepartmentSelectField`、`AttachmentField` 等宜搭原生字段组件不能因为表单 Schema 支持同名字段，就默认在 Code Canvas JSX 中可用。使用前必须确认：

- `importedModules` 包含可解析的宜搭组件库，例如 `@ali/deep` 或平台约定的字段组件包。
- Code Canvas 运行时有对应依赖映射和 CSS。
- 组件所需页面上下文、组织权限、弹层容器、移动端版本均可用。
- `onChange` 返回值结构能被后续数据保存或查询逻辑消费。

如果缺少任一证据，降级为普通 UI：用 antd 或自定义控件选择候选用户，值只存已知 userId / unionId / 文本快照，不伪造成原生字段组件。

## EmployeeField 验证示例

示例只用于 Code Canvas 验证，不要复制到普通 `.oyd.jsx` 自定义页：

```jsx
import React, { useState } from 'react';
import { EmployeeField } from '@ali/deep';

function YidaComp(props) {
  var ReactRef = React;
  var state = ReactRef.useState([]);
  var value = state[0];
  var setValue = state[1];

  return (
    <div style={{ padding: 16 }}>
      <EmployeeField
        label="负责人"
        placeholder="请选择负责人"
        multiple={false}
        value={value}
        onChange={(nextValue) => {
          console.log('employee value', nextValue);
          setValue(nextValue || []);
        }}
      />
      <pre style={{ marginTop: 12, fontSize: 12 }}>
        {JSON.stringify(value, null, 2)}
      </pre>
    </div>
  );
}

export default YidaComp;
```

验收点：

- 设计器属性中 `importedModules` 是否包含 `@ali/deep` 或等价字段组件库。
- 页面控制台没有 `EmployeeField is not defined`、样式缺失、弹层挂载错误。
- 成员弹层能打开、搜索、选择、清空。
- PC 和移动端都能完成选择。
- `onChange` 输出的值结构被记录下来，后续代码按真实结构处理。
