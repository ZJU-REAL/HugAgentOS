# 自定义页面运行时护栏

本页承接 `SKILL.md` 的规则摘要，说明哪些问题应在生成代码时避免、哪些由 `openyida check-page` 阻塞、哪些由兼容层自动修复。目标是在源头减少错误，同时保留发布前的兜底能力。

## 生成代码前必查

| 规则 | 要求 | check-page / 兼容层 |
|------|------|---------------------|
| `pageSize` | 列表、看板、分页接口默认使用 `pageSize: 50`，最大 100 | `>100` 报 `page-size-limit`；兼容层会钳制为 100；缺省分页对象会补 50 |
| loading 恢复 | 所有接口失败、超时、返回结构异常路径都必须写回 `loading: false`，并展示空态或错误态 | lint 只检查 `.catch()`，生成代码时必须主动覆盖失败路径 |
| 业务状态 | 业务态只写 `_customState`，通过 `setCustomState()` / `forceUpdate()` 触发渲染 | `this.setState({业务字段})` 报 `setState-non-timestamp` warning |
| ECharts DOM 时序 | `echarts.init` 必须在容器渲染后执行，推荐 `setTimeout(..., 300)` 或 `requestAnimationFrame` | 直接 `echarts.init(...)` 报 `echarts-dom-ready` warning |
| `renderJsx` 绑定 | `renderJsx` 顶部声明 `var self = this;`，事件和数组回调用箭头函数包裹 | 兼容层会插入 self，并自动修复部分事件/数组回调 |
| 动态对象 | 禁止对象字面量计算属性名 `{ [key]: value }` | `computed-property` error 阻塞发布 |
| 输入控件 | `<input>` 使用 `defaultValue` + `onChange` 静默写 `_customState` | `controlled-input` error 阻塞发布 |
| 可见下拉 | 用户可见下拉不要使用原生 `<select>` | `native-select-ui` warning；组件写法见 `component-jsx-guide.md` |
| 字符串补零 | 不用 `padStart` / `padEnd`，改成三元拼接 | `pad-method` warning |

## 生成列表 / 看板页面的默认状态

列表、看板和图表页的 `_customState` 至少包含：

- `loading`
- `list` 或 `cards`
- `currentPage`
- `pageSize`
- `totalCount`
- `filters`
- `errorMessage`
- `dialogVisible`
- `selectedRecord`

接口成功时更新数据、分页和统计；接口失败时写入空列表、`totalCount: 0`、错误信息，并把 `loading` 置回 `false`。

## 图表初始化顺序

ECharts 只在数据进入 `_customState` 并完成一次 `forceUpdate()` 后初始化。推荐顺序：

1. `didMount()` 加载 ECharts 脚本和业务数据。
2. 数据返回后更新 `_customState` 并 `forceUpdate()`。
3. 使用 `setTimeout(function() { self.renderChart(); }, 300)` 等待容器挂载。
4. `renderChart()` 中先判断容器存在，再 `echarts.init` / `setOption`。

如果页面支持刷新，刷新后要重新计算 option；已有 chart 实例时优先 `setOption`，不要重复创建多个实例。

## 发布前验证链路

每次发布前按顺序执行：

```bash
openyida check-page project/pages/src/<页面名>.oyd.jsx --json
openyida compile project/pages/src/<页面名>.oyd.jsx
```

`check-page` 有 error 时必须修复；warning 中涉及运行时稳定性的规则（`setState-non-timestamp`、`echarts-dom-ready`、`page-size-recommend`、`native-select-ui`）也应在生成代码阶段主动修正。
