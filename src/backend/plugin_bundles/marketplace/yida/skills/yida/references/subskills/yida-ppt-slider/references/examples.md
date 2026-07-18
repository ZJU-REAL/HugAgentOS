# yida-ppt-slider 使用示例

## 示例 1：创建产品路演幻灯片

### 输入

用户要求：在宜搭内创建一个产品路演 PPT，包含封面、目录、3 个功能介绍页和结束页。

### 执行步骤

```bash
# Step 1：检测环境
openyida env

# Step 2：创建应用（如已有应用则跳过）
openyida create-app "产品演示中心"
# 输出：appType = APP_DEMO123

# Step 3：创建自定义页面
openyida create-page APP_DEMO123 "产品路演2026"
# 输出：formUuid = FORM-PPT001

# Step 4：编写幻灯片代码（见下方代码示例）
# 输出到 project/pages/src/product-ppt.js

# Step 5：校验并发布页面
openyida check-page project/pages/src/product-ppt.js
openyida publish project/pages/src/product-ppt.js APP_DEMO123 FORM-PPT001 --health-check
```

### 输出

```json
{
  "success": true,
  "pageUrl": "https://www.aliwork.com/APP_DEMO123/custom/FORM-PPT001"
}
```

---

## 示例 2：幻灯片页面核心代码

### 幻灯片数据定义

```javascript
// ── 幻灯片数据（必须定义为顶层常量，不能硬编码在 renderJsx 中）────
var SLIDES = [
  {
    type: 'cover',
    bg: '#ffffff',
    accent: '#0089ff',
    eyebrow: '2026 产品发布',
    title: 'OpenYida 2.0',
    subtitle: 'AI 驱动的低代码开发平台',
    tags: ['AI 原生', '低代码', '企业级'],
  },
  {
    type: 'toc',
    bg: '#ffffff',
    accent: '#0089ff',
    title: '目录',
    items: ['核心能力', '应用场景', '技术架构', '开始使用'],
  },
  {
    type: 'key-points',
    bg: '#ffffff',
    accent: '#0089ff',
    chapter: '01 核心能力',
    title: 'AI 对话驱动开发',
    subtitle: '一句话生成完整应用',
    points: [
      { icon: '🤖', title: 'AI 表单设计', desc: '描述需求，自动生成字段结构' },
      { icon: '⚡', title: '秒级发布', desc: '从需求到上线，最快 5 分钟' },
      { icon: '🔗', title: '全流程覆盖', desc: '创建→配置→发布→数据管理' },
    ],
  },
  {
    type: 'ending',
    bg: '#0089ff',
    accent: '#ffffff',
    title: '立即体验',
    subtitle: 'npm install -g openyida',
    quote: '让 AI 成为你的低代码开发伙伴',
  },
];

// ── 状态（含新增能力字段）─────────────────────────────────────
var _customState = {
  currentIndex: 0,
  navVisible: false,     // 导航栏默认隐藏
  isFullscreen: false,   // 全屏状态
  lang: 'zh',            // 当前语言
  themeMode: 'light',    // 主题模式：'light' 或 'dark'
  numBuffer: '',          // 数字键输入缓冲
  numTimer: null,         // 数字键延迟定时器
};
```

### 国际化文案定义

```javascript
// ── 国际化文案（中英文双语）──────────────────────────────────
var I18N = {
  zh: {
    prev: '← 上一页',
    next: '下一页 →',
    pageOf: function(c, t) { return c + ' / ' + t; },
    fullscreen: '全屏',
    exitFullscreen: '退出全屏',
    langSwitch: 'EN',
    themeLight: '浅色',
    themeDark: '深色',
  },
  en: {
    prev: '← Prev',
    next: 'Next →',
    pageOf: function(c, t) { return c + ' / ' + t; },
    fullscreen: 'Fullscreen',
    exitFullscreen: 'Exit Fullscreen',
    langSwitch: '中',
    themeLight: 'Light',
    themeDark: 'Dark',
  },
};

// ── 主题配置（深色/浅色配色方案）──────────────────────────────
var THEME_CONFIG = {
  light: {
    bg: '#ffffff',
    text: '#1a1a2e',
    textSecondary: 'rgba(26,26,46,0.7)',
    border: 'rgba(26,26,46,0.08)',
    toolbarBg: 'rgba(26,26,46,0.06)',
    navBg: 'linear-gradient(transparent, rgba(0,0,0,0.05))',
  },
  dark: {
    bg: '#1a1a2e',
    text: '#ffffff',
    textSecondary: 'rgba(255,255,255,0.7)',
    border: 'rgba(255,255,255,0.1)',
    toolbarBg: 'rgba(255,255,255,0.1)',
    navBg: 'linear-gradient(transparent, rgba(255,255,255,0.05))',
  },
};
```

### 生命周期与事件绑定

```javascript
// ✅ 使用 didMount 注册键盘事件（宜搭运行时会在页面挂载后调用）
export function didMount() {
  var self = this;
  _customState.total = SLIDES.length;

  // 键盘翻页（支持演讲笔 PageDown/PageUp + 数字键快速跳页）
  this._handleKeyDown = function(e) {
    // ── 数字键快速跳页（支持双位数延迟缓冲）──
    var digit = null;
    if (e.key >= '0' && e.key <= '9') {
      digit = e.key;
    }
    if (digit !== null) {
      if (_customState.numTimer) {
        clearTimeout(_customState.numTimer);
      }
      _customState.numBuffer += digit;
      _customState.numTimer = setTimeout(function() {
        var targetIndex = parseInt(_customState.numBuffer, 10) - 1;
        if (targetIndex >= 0 && targetIndex < SLIDES.length) {
          _customState.currentIndex = targetIndex;
          self.forceUpdate();
        }
        _customState.numBuffer = '';
        _customState.numTimer = null;
      }, 300);
      return;
    }

    // ── 方向键 / 演讲笔翻页 ──
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown' || e.key === 'PageDown') {
      if (_customState.currentIndex < SLIDES.length - 1) {
        _customState.currentIndex++;
        self.forceUpdate();
      }
    } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp' || e.key === 'PageUp') {
      if (_customState.currentIndex > 0) {
        _customState.currentIndex--;
        self.forceUpdate();
      }
    }
  };
  document.addEventListener('keydown', this._handleKeyDown);

  // ── 鼠标移到底部显示导航栏 ──
  this._handleMouseMove = function(e) {
    var isNearBottom = e.clientY > window.innerHeight - 80;
    if (isNearBottom !== _customState.navVisible) {
      _customState.navVisible = isNearBottom;
      self.forceUpdate();
    }
  };
  document.addEventListener('mousemove', this._handleMouseMove);

  // ── 全屏状态变化监听 ──
  this._handleFullscreenChange = function() {
    _customState.isFullscreen = !!document.fullscreenElement;
    self.forceUpdate();
  };
  document.addEventListener('fullscreenchange', this._handleFullscreenChange);

  // ── URL hash 定位初始化 ──
  this._handleHashChange = function() {
    var hash = window.location.hash;
    if (hash && hash.startsWith('#')) {
      var pageNum = parseInt(hash.substring(1), 10);
      var targetIndex = pageNum - 1;
      if (targetIndex >= 0 && targetIndex < SLIDES.length) {
        _customState.currentIndex = targetIndex;
        self.forceUpdate();
      }
    }
  };
  this._handleHashChange();
  window.addEventListener('hashchange', this._handleHashChange);
}

export function didUnmount() {
  document.removeEventListener('keydown', this._handleKeyDown);
  document.removeEventListener('mousemove', this._handleMouseMove);
  document.removeEventListener('fullscreenchange', this._handleFullscreenChange);
  window.removeEventListener('hashchange', this._handleHashChange);
  if (_customState.numTimer) {
    clearTimeout(_customState.numTimer);
  }
}

export function getCustomState(key) { if (key) return _customState[key]; return _.clone(_customState); }
export function setCustomState(newState) { _.assign(_customState, newState); this.forceUpdate(); }
export function forceUpdate() { this.setState({ timestamp: new Date().getTime() }); }
```

### 渲染函数

```javascript
export function renderJsx() {
  var state = _customState;
  var slide = SLIDES[state.currentIndex];
  var isMobile = this.utils.isMobile();
  var self = this;
  var lang = I18N[state.lang] || I18N.zh;
  var theme = THEME_CONFIG[state.themeMode] || THEME_CONFIG.light;

  var handlePrev = function() {
    if (state.currentIndex > 0) {
      state.currentIndex--;
      window.location.hash = state.currentIndex + 1;
      self.forceUpdate();
    }
  };
  var handleNext = function() {
    if (state.currentIndex < SLIDES.length - 1) {
      state.currentIndex++;
      window.location.hash = state.currentIndex + 1;
      self.forceUpdate();
    }
  };
  var handleToggleFullscreen = function() {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen().catch(function() {});
    } else {
      document.exitFullscreen().catch(function() {});
    }
  };
  var handleLangSwitch = function() {
    state.lang = state.lang === 'zh' ? 'en' : 'zh';
    self.forceUpdate();
  };
  var handleThemeToggle = function() {
    state.themeMode = state.themeMode === 'light' ? 'dark' : 'light';
    self.forceUpdate();
  };

  // ── 导航栏样式（默认隐藏，鼠标移到底部显示）──
  var navStyle = {
    position: 'absolute', bottom: 0, left: 0, right: 0,
    padding: '16px 0',
    display: 'flex', justifyContent: 'center', alignItems: 'center', gap: '16px',
    background: 'linear-gradient(transparent, rgba(0,0,0,0.05))',
    opacity: state.navVisible ? 1 : 0,
    transform: state.navVisible ? 'translateY(0)' : 'translateY(10px)',
    transition: 'opacity 0.3s ease, transform 0.3s ease',
    pointerEvents: state.navVisible ? 'auto' : 'none',
  };

  return (
    <div style={{
      position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
      background: theme.bg, color: theme.text, overflow: 'hidden',
      padding: '0 !important', borderRadius: '0 !important',
    }}>
      <div style={{ display: 'none' }}>{this.state.timestamp}</div>

      {/* ── 右上角工具栏：主题切换 + 语言切换 + 全屏按钮 ── */}
      <div style={{ position: 'absolute', top: '16px', right: '16px', display: 'flex', gap: '8px', zIndex: 100 }}>
        <div
          onClick={function() { handleThemeToggle(); }}
          style={{
            width: '36px', height: '36px', borderRadius: '8px',
            background: theme.toolbarBg,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            cursor: 'pointer', fontSize: '18px',
          }}
          title={state.themeMode === 'light' ? lang.themeDark : lang.themeLight}
        >
          {state.themeMode === 'light' ? '🌙' : '☀️'}
        </div>
        <div
          onClick={function() { handleLangSwitch(); }}
          style={{
            height: '36px', padding: '0 12px', borderRadius: '8px',
            background: theme.toolbarBg,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            cursor: 'pointer', fontSize: '13px', fontWeight: 600, color: theme.text,
          }}
        >
          {lang.langSwitch}
        </div>
        <div
          onClick={function() { handleToggleFullscreen(); }}
          style={{
            width: '36px', height: '36px', borderRadius: '8px',
            background: theme.toolbarBg,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            cursor: 'pointer', fontSize: '16px',
          }}
          title={state.isFullscreen ? lang.exitFullscreen : lang.fullscreen}
        >
          {state.isFullscreen ? '⊡' : '⛶'}
        </div>
      </div>

      {/* ── 幻灯片内容区 ── */}
      <div style={{ padding: isMobile ? '20px 16px' : '48px 80px', height: '100%' }}>
        <h1 style={{ fontSize: isMobile ? '28px' : '48px', color: theme.text, fontWeight: 800 }}>
          {slide.title}
        </h1>
        {slide.subtitle && (
          <p style={{ fontSize: isMobile ? '16px' : '24px', color: theme.textSecondary, marginTop: '16px' }}>
            {slide.subtitle}
          </p>
        )}
      </div>

      {/* ── 导航栏（默认隐藏，鼠标移到底部显示）── */}
      <div style={navStyle}>
        <button onClick={function() { handlePrev(); }} style={{ padding: '8px 20px', borderRadius: '20px', border: '1px solid ' + theme.border, cursor: 'pointer', background: theme.bg, color: theme.text }}>
          {lang.prev}
        </button>
        <span style={{ lineHeight: '36px', color: theme.textSecondary }}>{lang.pageOf(state.currentIndex + 1, SLIDES.length)}</span>
        <button onClick={function() { handleNext(); }} style={{ padding: '8px 20px', borderRadius: '20px', border: 'none', cursor: 'pointer', background: slide.accent, color: '#fff' }}>
          {lang.next}
        </button>
      </div>
    </div>
  );
}
```

### 注意事项

- 事件处理函数推荐在 `renderJsx` 顶部定义；JSX 中可以直接引用 handler，或用不依赖 `this` 的小包装函数调用 handler。禁止 `onClick={foo()}` 渲染期调用、禁止 JSX 小写 `onclick`
- 必须在 `didUnmount` 中清理所有事件监听（键盘、鼠标移动、全屏变化、hash 变化、数字键定时器），防止内存泄漏
- 使用 `position: fixed` 覆盖宜搭默认容器样式
- 图片使用 `objectFit: 'contain'` 确保完整显示
- **数字键翻页**：使用 300ms 延迟缓冲区分单位数和双位数输入（如连按 `1` `2` = 第 12 页）
- **导航栏默认隐藏**：通过 `navVisible` 状态 + `mousemove` 事件控制，鼠标移到底部 80px 区域时显示
- **全屏按钮**：使用 Fullscreen API，需在用户点击事件中调用，部分浏览器限制非手势触发
- **中英文切换**：UI 文案从 `I18N[state.lang]` 动态读取，切换后调用 `forceUpdate()` 刷新
- **深色/浅色模式**：使用 `THEME_CONFIG` 定义配色方案，所有颜色从 `theme` 对象动态获取，切换后调用 `forceUpdate()` 刷新
- **URL hash 定位**：翻页时同步更新 `window.location.hash`（格式为 `#页码`），支持浏览器前进后退和分享链接
- **ECharts Bar Chart Race**：新增 `echarts-race` 类型幻灯片，通过阿里 CDN 加载 ECharts 5.6.0，使用逐帧线性插值实现平滑动画
