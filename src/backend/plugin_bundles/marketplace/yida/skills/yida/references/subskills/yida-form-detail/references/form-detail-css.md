# 默认表单详情页 CSS

## 改色速查

优先改顶部 CSS 变量，不要先改选择器结构。

| 变量 | 控制范围 |
| --- | --- |
| `--yida-form-container-bgcolor-custom` | 宜搭表单容器背景色 |
| `--oyd-detail-page-bg` | 详情页整体背景 |
| `--oyd-detail-card-bg` | 页头、详情区、评论区、操作栏背景 |
| `--oyd-detail-border` | 卡片边框颜色 |
| `--oyd-detail-radius` | 卡片圆角 |
| `--oyd-detail-max-width` | 页面内容最大宽度 |
| `--oyd-detail-value-bg` | 字段值标注块背景 |
| `--oyd-detail-value-border` | 字段值标注块左边框 |

## 完整 CSS

```css
/* =========================================
   yida-form-detail detail page style v1.4
   ========================================= */
:root {
  --yida-form-container-bgcolor-custom: #f6f7f9;
  --form-element-label-line-height: 28px;
  --oyd-detail-page-bg: #f6f7f9;
  --oyd-detail-card-bg: #ffffff;
  --oyd-detail-border: #e5e6e8;
  --oyd-detail-radius: 20px;
  --oyd-detail-gap: 12px;
  --oyd-detail-max-width: 1440px;
  --oyd-detail-label-color: rgba(24, 32, 51, 0.72);
  --oyd-detail-value-color: #182033;
  --oyd-detail-value-bg: rgba(247, 248, 250, 0.72);
  --oyd-detail-value-border: rgba(131, 137, 143, 0.24);
}

/* 页面背景与内容宽度 */
.vc-page-yida-page.vc-page.yida-formDetail {
  background-color: var(--oyd-detail-page-bg) !important;
  padding-left: var(--oyd-detail-gap) !important;
  padding-right: var(--oyd-detail-gap) !important;
}

.vc-page-content-1180 .vc-rootcontent,
.top-banner-area.pc-1200,
.view-detail-footer,
.stickyFooter.is-sticky {
  max-width: var(--oyd-detail-max-width) !important;
}

/* 页头区域 */
.top-banner-area.pc-1200 {
  width: calc(100% - 24px) !important;
  margin: 12px auto 0 !important;
  border-radius: var(--oyd-detail-radius) !important;
  border: 1px solid var(--oyd-detail-border) !important;
  background-color: var(--oyd-detail-card-bg) !important;
}

.top-banner-area .yida-container.pc-1180 {
  max-width: none !important;
}

/* 详情区域 */
.vc-deep-container-entry.vc-rootcontent {
  border-radius: var(--oyd-detail-radius) !important;
  border: 1px solid var(--oyd-detail-border) !important;
  background-color: var(--oyd-detail-card-bg) !important;
  --yida-form-content-margin: 12px;
}

/* 评论区域 */
.view-detail-footer {
  width: calc(100% - 24px) !important;
  margin: 12px auto 0 !important;
  border-radius: var(--oyd-detail-radius) !important;
  border: 1px solid var(--oyd-detail-border) !important;
  background-color: var(--oyd-detail-card-bg) !important;
}

.view-detail-title {
  box-sizing: border-box !important;
  height: 48px !important;
  padding: 24px 24px !important;
  border-bottom: 1px solid rgba(31, 56, 88, 0) !important;
  color: #182033 !important;
  font-family: PingFangSC-Medium, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif !important;
  font-size: 16px !important;
  line-height: 24px !important;
  opacity: 0.86 !important;
}

.view-detail-footer .next-tabs-capsule > .next-tabs-bar .next-tabs-tab:first-child {
  border-radius: 20px 0 0 20px !important;
}

.view-detail-footer .next-tabs-capsule > .next-tabs-bar .next-tabs-tab:last-child {
  border-radius: 0 20px 20px 0 !important;
}

.next-tabs-tab-inner {
  text-align: center !important;
}

/* 底部操作栏 */
.stickyFooter.is-sticky {
  width: calc(100% - 24px) !important;
  height: 56px !important;
  margin: 0 auto 12px !important;
  border-radius: 30px !important;
  border: 1px solid var(--oyd-detail-border) !important;
  background-color: var(--oyd-detail-card-bg) !important;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.16) !important;
}

.deep-button-group-item.next-btn.btn-weight.separated {
  border-radius: 8px !important;
}

/* 字段标签与字段值 */
.next-form-item-label,
.next-form-item-label label,
.next-form-preview.next-form-item.next-medium .next-form-item-label,
.vc-page-yida-page .next-form-item .next-form-item-label {
  color: var(--oyd-detail-label-color) !important;
  line-height: 20px !important;
}

.next-form-preview {
  color: var(--oyd-detail-value-color) !important;
}

/* 字段值标注块，人员字段通过 .employee 排除 */
.next-form-item-control > .next-form-preview:not(.employee) {
  min-height: 28px !important;
  padding: 0 8px !important;
  border-radius: 8px !important;
  border-left: 2px solid var(--oyd-detail-value-border) !important;
  background-color: var(--oyd-detail-value-bg) !important;
  overflow: hidden !important;
}

/* 人员字段 */
.responsive-tags-item {
  border-radius: 8px !important;
  border-left: 2px solid var(--oyd-detail-value-border) !important;
  background-color: var(--oyd-detail-value-bg) !important;
}

.deep-employee-form-field .next-form-preview.employee .employee-add-avatar img {
  border-radius: 8px !important;
}

/* 图片和附件字段重置 */
.next-upload-list-item {
  border: 0 !important;
  background-color: transparent !important;
}

.vc-page-yida-page.vc-page .next-form-item.imageField .next-form-item-control > .next-form-preview,
.vc-page-yida-page.vc-page .next-form-item.attachmentField .next-form-item-control > .next-form-preview {
  padding: 0 !important;
  border-left: none !important;
  border-radius: 0 !important;
  background-color: transparent !important;
  overflow: visible !important;
}

@media (max-width: 768px) {
  :root {
    --oyd-detail-radius: 12px;
    --oyd-detail-gap: 8px;
  }

  .top-banner-area.pc-1200,
  .view-detail-footer,
  .stickyFooter.is-sticky {
    width: calc(100% - 16px) !important;
  }

  .view-detail-title {
    padding: 16px !important;
  }
}
```

## 局部抽取建议

- 只优化页头：保留 `:root`、页面背景与宽度、页头区域三段。
- 只优化字段值：保留 `:root`、字段标签与字段值、字段值标注块、人员字段、图片附件重置。
- 只优化评论区：保留 `:root`、评论区域和 tabs 三段。
- 只优化操作栏：保留 `:root` 和底部操作栏两段。
