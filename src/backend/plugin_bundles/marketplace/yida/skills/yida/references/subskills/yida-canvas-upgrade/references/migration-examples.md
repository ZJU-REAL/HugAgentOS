# Canvas 升级：转换模式示例与报告模板

本文件承载 `yida-canvas-upgrade` 的完整代码示例与升级报告模板，SKILL.md 只保留步骤与链接。

## 转换模式示例

普通页面（native `.oyd.jsx`，`_customState` + `forceUpdate` + `renderJsx`）：

```jsx
var _customState = { count: 0 };

export function add() {
  _customState.count += 1;
  this.forceUpdate();
}

export function renderJsx() {
  var self = this;
  return <button onClick={(e) => { self.add(); }}>{_customState.count}</button>;
}
```

Code Canvas（真 React18 hooks，导出 `YidaComp`）：

```jsx
import React, { useState } from 'react';

function YidaComp(props) {
  var state = useState(0);
  var count = state[0];
  var setCount = state[1];

  return (
    <button onClick={() => { setCount(count + 1); }}>
      {count}
    </button>
  );
}

export default YidaComp;
```

转换要点：
- `_customState` 字段 → `useState`；复杂派生数据 → `useMemo`。
- `didMount` / `didUnmount` → `useEffect(() => { ...; return cleanup; }, [])`。
- `this.forceUpdate()` / `setCustomState` 强刷 → 删除，靠 hooks 自然重渲染。
- `this.utils.yida.*` / `this.dataSourceMap.*` 无法照搬，抽成 props 调用或明确 TODO，不要假装可用（Canvas 无平台数据桥）。

## 升级报告模板

迁移完成或受阻时，输出简短报告：

```markdown
## Canvas Upgrade Report

- Source page: project/pages/src/xxx.oyd.jsx
- Target page: APP_XXX / FORM-XXX
- Upgrade status: ready | blocked | partially-ready
- Generated canvas source: project/pages/src/xxx.canvas.jsx
- Required dependencies: react, antd, ...
- Schema write path: designer | verified-api | not-available
- Blockers:
  - ...
- Verification:
  - [ ] YidaCodeCanvas exists in schema
  - [ ] importedModules contains required dependencies
  - [ ] first screen renders
  - [ ] core interactions work
```
