# 素材资源指南

在自定义页面开发中，经常需要使用图片、音乐/音效、Icon 等素材资源。以下是推荐的素材获取方案，确保素材来源稳定、合规、风格一致。

## 图片素材

| 素材库 | API | 授权方式 | 推荐场景 |
| --- | --- | --- | --- |
| [Unsplash](https://unsplash.com) | ✅ | 免费商用，无需署名 | 高质量背景图、Banner、配图 |
| [Pexels](https://pexels.com) | ✅ | 免费商用，无需署名 | 人物、场景、商务类配图 |
| [Pixabay](https://pixabay.com) | ✅ | 免费商用，无需署名 | 插画、矢量图、通用配图 |
| [Lorem Picsum](https://picsum.photos) | ✅ | 免费 | 开发阶段占位图 |
| [Wikimedia Commons](https://commons.wikimedia.org) | ⚠️ | 授权类型多样，需按条目核对 | 知识类/历史类配图 |

## 音乐/音效素材

| 素材库 | 授权方式 | 署名要求 | 推荐场景 |
| --- | --- | --- | --- |
| [Pixabay Music](https://pixabay.com/music/) | 免费商用 | 无需 | 背景音乐、氛围音效 |
| [Mixkit](https://mixkit.co/free-sound-effects/) | 免费商用 | 无需 | 短音效、UI 交互音 |
| [Freesound](https://freesound.org) | CC0 / CC BY | ⚠️ 部分需署名 | 按钮音效、提示音、环境音 |
| [Incompetech](https://incompetech.com/music/) | CC BY 4.0 | ⚠️ 需署名 Kevin MacLeod | 游戏、活动页背景音乐 |
| [Free Music Archive](https://freemusicarchive.org) | 多种 CC | ⚠️ 需按条目核对 | 曲库大，适合按分类批量拉取 |

- 优先使用 Pixabay Music 和 Mixkit（无署名要求）
- 使用 CC BY 素材时，需在页面底部添加署名，格式：`Music: "曲名" by 作者 — Licensed under CC BY 4.0`
- 音频文件建议上传到 CDN，移动端使用压缩后的 MP3 格式

## Icon 素材

> 优先使用 `this.utils.loadStyleSheet(url)` 加载 CSS 图标库，详见 [yida-api.md](../../../references/yida-api.md) 的「loadStyleSheet」章节。

| 图标库 | 授权方式 | 推荐场景 |
| --- | --- | --- |
| [iconfont（阿里）](https://www.iconfont.cn) | 免费 | **首选**，国内访问最稳定，支持自定义图标集 |
| [Remix Icon](https://remixicon.com) | Apache 2.0 | 开源免费，风格现代，无需注册 |
| [Font Awesome](https://fontawesome.com) | MIT（免费版） | 覆盖面广，通用 UI 图标 |
| [Material Icons](https://fonts.google.com/icons) | Apache 2.0 | 数量大，适合中后台工具类产品 |
| [Bootstrap Icons](https://icons.getbootstrap.com) | MIT | 轻量，SVG 为主 |
| [Heroicons](https://heroicons.com) | MIT | 线性/实心两套，现代极简风 |

**SVG 内联**（少量图标，无外部依赖）：

```javascript
function renderIcon(iconPath, size, color) {
  return (
    <svg width={size || 24} height={size || 24} viewBox="0 0 24 24" fill="none" stroke={color || 'currentColor'} strokeWidth="2">
      <path d={iconPath} />
    </svg>
  );
}
```

## 素材使用通用建议

### 稳定性
- 生产环境的图片/音频应上传到自有 CDN，避免第三方外链失效
- 图片/音频优先使用有官方 API 的站点（Unsplash / Pexels / Pixabay / Freesound），避免爬虫方式
- 同一关键词可并行查 2-3 个库，失败自动切换；对外链下载做本地缓存

### 合规性
- 优先使用无署名要求的素材库（Unsplash、Pexels、Pixabay、Mixkit）
- 使用 CC BY 素材时必须添加署名，至少记录以下字段：`source`（来源站点）、`author`（作者）、`license`（许可证类型）、`requiredAttribution`（是否需要署名）、`sourceUrl`（原始链接）
- Wikimedia Commons / Freesound / FMA 等站点授权类型多样，务必按条目核对 License

### 一致性
- 同一项目中统一使用一个图标库，避免混用多个图标库导致风格不一致
- 准备「语义→图标名」映射表（如 `search → ri-search-line`、`settings → ri-settings-3-line`），避免随机挑选

### 性能
- 图片使用合适尺寸（避免加载 4K 大图）；音频使用压缩后的 MP3 格式
- 图标优先使用 CDN 字体方案（iconfont / Remix Icon），少量图标可用 SVG 内联

---

## CDN 安全规范

> ⚠️ **安全风险警告：禁止引用未知或不可信的 CDN 地址**
>
> 引用来源不明的第三方 CDN 链接（如随意从搜索结果或论坛复制的 JS/CSS 链接）存在严重安全隐患：
>
> - **网络劫持风险**：不可信 CDN 可能被中间人攻击，注入恶意脚本，导致用户数据泄露或页面被篡改
> - **供应链攻击**：第三方 CDN 资源随时可能被替换为恶意内容，且难以察觉
> - **服务不稳定**：免费 CDN 可能随时宕机或 SSL 证书过期，导致页面资源加载失败

**安全规范：**
- ✅ 生产默认只推荐阿里云 CDN（`g.alicdn.com` / `alicdn.com`）或客户企业自托管 CDN，国内访问最稳定，适合宜搭客户环境
- ✅ 引用 `g.alicdn.com` 资源前必须验证 URL 返回 `200` 且 `content-type` 符合资源类型；不要把未验证路径写入模板
- ✅ Tailwind browser 默认使用已验证地址：`https://g.alicdn.com/code/lib/tailwindcss-browser/0.0.0-insiders.fed6c6a/index.global.min.js`
- ✅ 引用第三方 CDN 资源时，建议添加 `integrity` 属性（SRI 校验），防止资源被篡改
- ⚠️ `cdnjs.cloudflare.com`、`unpkg.com` 等海外 CDN 只能作为本地调试或路径排查参考，不要写入默认生成模板，避免客户网络慢或不可达
- ❌ **禁止使用 `cdn.jsdelivr.net`**：cdn.jsdelivr.net 存在已知安全风险，禁止在生产环境使用。优先选择阿里云 CDN 或项目已配置的 CDN 域名。
- ❌ **禁止使用 `fonts.googleapis.com`**：国内大陆无法访问，字体资源需下载到本地后上传自有 CDN
- ❌ 禁止引用来源不明的 CDN 地址，即使该链接当前可以正常访问
- ❌ 禁止直接使用从搜索引擎、论坛、博客中复制的 CDN 链接，需先核实来源
