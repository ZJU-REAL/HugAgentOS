
# yida-ppt 已废弃

本技能只保留兼容入口。所有宜搭 PPT / 幻灯片 / 演示文稿开发都应改用：

```text
use_skill("yida-ppt-slider", "开发宜搭全屏幻灯片页面")
```

## 执行规则

- 用户要求在宜搭中做 PPT、幻灯片、路演页或全屏演示页时，立即切换到 `yida-ppt-slider`。
- 如果用户明确说要旧版 `dark-tech` 风格，也使用 `yida-ppt-slider` 的 `dark-tech` 主题。
- 不要从本技能生成任何页面代码。
- 不要继续复制旧版内联 Canvas / 动画模板；这些实现已收敛到 `yida-ppt-slider`。

## 回复用户时

可以简短说明：

```text
yida-ppt 已合并到 yida-ppt-slider，我会使用新版 yida-ppt-slider 的对应主题来实现。
```

然后调用 `use_skill("yida-ppt-slider", "开发宜搭全屏幻灯片页面")` 并继续。
