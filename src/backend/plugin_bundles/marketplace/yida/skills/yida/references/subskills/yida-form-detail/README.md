
# 宜搭表单详情页样式优化

## 目标

优化宜搭表单详情页 `formDetail` 的默认视觉效果，覆盖页头、详情区域、评论区、底部操作栏和字段预览块。默认方案使用 20px 圆角卡片、12px 页面间距、1440px 最大宽度、字段值标注块和胶囊操作栏。

## 何时使用

- 用户说“表单详情页美化”“详情页优化”“formDetail 样式”“字段详情页不好看”。
- 新建应用包含表单，且用户希望统一详情页风格。
- 只调整表单详情页，不改自定义展示页面、不改数据记录。

## 不要这样做

- 不要把 CSS 写到表单页面 JS 的 `didMount`，`formDetail` 不执行这类页面 JS。
- 不要用 `RichTextField` 承载样式，设计器可能提示组件未找到。
- 不要把承载 CSS 的 Html 组件设为 `hidden: true`，否则 `<style>` 不会进入 DOM。
- 不要用 `openyida publish` 发布这个样式；它不是自定义页面源码。
- 不要编造 `appType`、`formUuid` 或 `fieldId`。缺失时先从命令输出、缓存或 `openyida get-schema` 获取。

## 推荐方案

通过表单 Schema 在 `FormContainer` 首位注入或更新一个宜搭原生 `Html` 组件：

- 组件 id 固定为 `yida-form-detail-css-html`，便于幂等更新。
- `props.content` 写入 `<style>...</style>`。
- `props.__style__` 使用 `height: 0px`、`overflow: hidden`，避免占用页面空间。
- `hidden` 必须为 `false`，`isLocked` 建议为 `true`。
- 同步写入 `root.css` 作为兜底，但以 Html 组件为主要持久化方式。

完整步骤见 [注入流程](references/injection-guide.md)。完整默认 CSS 见 [默认样式](references/form-detail-css.md)。

## 执行流程

1. 确认当前登录态和组织：
   ```bash
   openyida env --json
   openyida login --check-only --json
   ```
2. 确认目标 `appType` 与表单 `formUuid`。如果用户只给了表单名，先用应用表单列表或 `openyida get-schema <appType> --all` 辅助定位。
3. 读取 [注入流程](references/injection-guide.md)，按步骤获取 Schema、注入 Html 组件、保存 Schema、刷新 `MINI_RESOURCE`。
4. 需要改色、改圆角或只优化局部时，读取 [默认样式](references/form-detail-css.md)，只调整对应变量或 CSS 分区。
5. 保存后再次获取 Schema，确认 `yida-form-detail-css-html` 存在，且 `props.content` 包含 `yida-form-detail` 版本注释。

## 决策规则

- 用户要求完整美化：使用默认 CSS 全量注入。
- 用户只要求某一区域：从默认 CSS 中截取对应分区，仍用同一个 Html 组件承载。
- 用户有品牌色：优先改 CSS 顶部变量，不要大面积改选择器。
- 新建应用完成后如包含表单：可以主动问用户是否要应用表单详情页优化样式。

## 后续可 CLI 化

如果后续要把该技能变成确定性命令，建议新增：

```bash
openyida form-detail-style apply <appType> <formUuid> [--css file] [--preset clean-card]
openyida form-detail-style remove <appType> <formUuid>
openyida form-detail-style check <appType> <formUuid>
```

命令实现应复用本技能的 Html 组件 id、CSS marker 和保存/刷新流程。
