# 输出：视觉方向决策块

> Step 6 自检通过后，输出如下结构（**纯方向，不含代码**），然后提示交给 `yida-custom-page` / `yida-canvas-custom-page` 落地。

```markdown
### 【视觉方向决策】

- **导航形态**：<导航可见（跟品牌融合）/ 导航隐藏 isRenderNav=false（视觉自立 + 自带导航壳，说明壳型）>
- **页面类型**：<workbench / dashboard / list / detail / landing>（判定依据一句话）
- **气质关键词**：<2-3 个>
- **项目特定设计原则**：<3-5 条，具体到业务>
- **布局骨架**：<来自 scene 文件的骨架，按本页信息调整>
- **信息密度**：<紧凑 / 均衡 / 宽松 + 一句理由>
- **视觉焦点**：<这页唯一的主角是什么>
- **场景专项策略**：<landing 写 Section 构图 + 素材锚点 + 转化动作；dashboard 写 Shell + Archetype + 数据洞察落点；其他场景按需写导航壳/多视图>

### 【差异化 5 维】

1. 辅助/强调色：<取法 + 从哪个气质推导>
2. 中性冷暖偏色：<冷灰 / 中性 / 暖灰 + 理由>
3. 圆角性格：<直角 / 微圆 / 大圆，全页统一>
4. 排版性格：<字重对比 / 字号跨度 / 字间距 / tabular-nums 用法>
5. 装饰母题（视觉 DNA）：<2-3 个贯穿全页的视觉基因>

### 【反默认说明】

<一句话：本方案与「统一灰白底 + 8px 圆角卡片 + 系统字体 + 蓝色强调」的默认脸在哪 ≥3 个维度不同>

### 【图标策略】

<内联 SVG 语义集（默认）/ 用户提供的 iconfont URL（opt-in）；描边风格；只作功能用途>

---
> 下一步：交 `yida-custom-page`（native）或 `yida-canvas-custom-page`（Canvas），按 `references/design-system.md` / `canvas-design-system.md` 的 token/组件把以上方向落成 JSX。
> 具体色值、圆角像素、间距、组件样式一律以 `design-system.md` 为准；本决策块只定方向与差异。
```
