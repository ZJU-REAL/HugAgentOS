# 深色科技风主题（dark-tech）完整参考

> 来源：`wukong-openyida-training-v2.js` 实战案例，适合企业培训、产品发布、科技感演示场景。

**设计体系：** 背景 `#0B0F19` · 主色蓝 `#3b82f6` / 紫 `#a855f7` / 绿 `#10b981` / 粉 `#ec4899`

## CSS 动画库（必须注入）

```javascript
var CSS_ANIMATIONS = [
  '@import url("https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@300;400;500;700;900&display=swap");',
  '@keyframes cineZoom{0%{opacity:0;transform:scale(1.4);filter:blur(30px) brightness(1.8)}40%{opacity:.7;transform:scale(1.08);filter:blur(8px) brightness(1.2)}100%{opacity:1;transform:scale(1);filter:blur(0) brightness(1)}}',
  '@keyframes cineParallax{0%{opacity:0;transform:translateX(-80px) scale(1.05);filter:blur(12px)}100%{opacity:1;transform:translateX(0) scale(1);filter:blur(0)}}',
  '@keyframes cineRise{0%{opacity:0;transform:translateY(60px) scale(.97);filter:blur(10px)}100%{opacity:1;transform:translateY(0) scale(1);filter:blur(0)}}',
  '@keyframes cineGlitch{0%{opacity:0;transform:skewX(-8deg) scale(1.1);filter:hue-rotate(90deg) blur(15px)}30%{opacity:.8;transform:skewX(4deg);filter:hue-rotate(0deg) blur(4px)}60%{transform:skewX(-2deg)}100%{opacity:1;transform:skewX(0) scale(1);filter:blur(0)}}',
  '@keyframes cineIris{0%{opacity:0;clip-path:circle(0% at 50% 50%);filter:blur(20px)}60%{clip-path:circle(60% at 50% 50%);filter:blur(4px)}100%{opacity:1;clip-path:circle(150% at 50% 50%);filter:blur(0)}}',
  '@keyframes cineGrand{0%{opacity:0;transform:scale(1.8);filter:blur(40px) brightness(2)}50%{opacity:.6;transform:scale(1.15);filter:blur(10px) brightness(1.3)}100%{opacity:1;transform:scale(1);filter:blur(0) brightness(1)}}',
  '@keyframes fadeIn{0%{opacity:0;transform:scale(.97)}100%{opacity:1;transform:scale(1)}}',
  '@keyframes titleCinematic{0%{opacity:0;transform:translateY(50px);filter:blur(10px);letter-spacing:12px}60%{letter-spacing:-1px}100%{opacity:1;transform:translateY(0);filter:blur(0);letter-spacing:-2px}}',
  '@keyframes subtitleCinematic{0%{opacity:0;transform:translateY(30px);filter:blur(8px)}100%{opacity:1;transform:translateY(0);filter:blur(0)}}',
  '@keyframes fu{0%{opacity:0;transform:translateY(30px)}100%{opacity:1;transform:translateY(0)}}',
  '@keyframes df{0%,100%{background-position:0% 50%}50%{background-position:100% 50%}}',
  '@keyframes gridMove{0%{background-position:0 0}100%{background-position:60px 60px}}',
  '@keyframes chapterGlow{0%,100%{opacity:.6;transform:scale(1)}50%{opacity:1;transform:scale(1.05)}}',
  '@keyframes tagSlideIn{0%{opacity:0;transform:translateY(-25px) scale(.8);filter:blur(6px)}100%{opacity:1;transform:translateY(0) scale(1);filter:blur(0)}}',
  ':-webkit-full-screen{width:100vw!important;height:100vh!important}',
  ':fullscreen{width:100vw!important;height:100vh!important}'
].join('\n');
```

## 转场配置

| 动画名 | 效果 | 适用场景 |
|--------|------|---------|
| `cineZoom` | 缩放+模糊+亮度 | 封面、重要开场 |
| `cineParallax` | 横向视差滑入 | 内容页 |
| `cineRise` | 从下方升起 | 总结、结尾 |
| `cineGlitch` | 故障艺术+色相旋转 | 技术页、震撼转场 |
| `cineIris` | 圆形光圈展开 | 章节切换 |
| `cineGrand` | 宏大缩放 | 高潮页、大数据 |
| `fadeIn` | 普通淡入缩放 | 普通内容页 |

```javascript
// 每张幻灯片的转场动画（key=幻灯片编号，value=动画名）
var slideTransitions = {
  1: 'cineZoom',      // 封面
  2: 'cineGlitch',   // 章节页
  3: 'cineParallax', // 内容页
  // ...
};
// 章节页编号（会额外渲染网格动画和光晕）
var chapterSlides = [2, 5, 9];
```

## 公共样式预设（S 对象）

```javascript
var S = {
  st:    { fontSize: '68px', fontWeight: 900, color: '#fff', marginBottom: '28px', letterSpacing: '-2px', lineHeight: 1.15, textShadow: '0 0 40px rgba(59,130,246,.5)', animation: 'titleCinematic 1.4s cubic-bezier(.25,.46,.45,.94) both' },
  stSts: { fontSize: '48px', fontWeight: 900, color: '#fff', marginBottom: '24px', letterSpacing: '-1px', lineHeight: 1.2, textShadow: '0 0 30px rgba(59,130,246,.4)' },
  ss:    { fontSize: '24px', fontWeight: 300, color: '#9ca3af', marginBottom: '40px', letterSpacing: '2px', animation: 'subtitleCinematic 1.2s ease .4s both' },
  tg:    { display: 'inline-block', background: 'rgba(59,130,246,.15)', border: '1px solid rgba(59,130,246,.3)', borderRadius: '20px', padding: '8px 24px', fontSize: '16px', color: '#60a5fa', marginBottom: '24px', letterSpacing: '2px', fontWeight: 500, animation: 'tagSlideIn .8s ease both' },
  gt:    { background: 'linear-gradient(90deg,#3b82f6,#a855f7,#10b981,#3b82f6)', backgroundSize: '300% 300%', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent', animation: 'df 6s ease infinite' },
  cd:    { background: 'rgba(255,255,255,.04)', backdropFilter: 'blur(20px)', WebkitBackdropFilter: 'blur(20px)', border: '1px solid rgba(255,255,255,.08)', borderRadius: '16px', padding: '28px 32px', textAlign: 'left' },
  ct:    { fontSize: '24px', fontWeight: 700, color: '#fff', marginBottom: '10px' },
  cx:    { fontSize: '18px', color: '#9ca3af', lineHeight: 1.7 },
  hl:    { color: '#3b82f6', fontWeight: 700 },
  hs:    { color: '#10b981', fontWeight: 700 },
  hp:    { color: '#a855f7', fontWeight: 700 },
  hw:    { color: '#f59e0b', fontWeight: 700 },
  hr:    { color: '#ec4899', fontWeight: 700 },
};
```

## Canvas 粒子系统

```javascript
export function initParticles() {
  var canvas = document.getElementById('ppt-particles');
  if (!canvas) return;
  var ctx = canvas.getContext('2d');
  var particles = [];
  var COUNT = 50;
  var DIST = 130;
  var colors = ['59,130,246', '147,51,234', '16,185,129'];

  function resize() { canvas.width = window.innerWidth; canvas.height = window.innerHeight; }
  function init() {
    resize(); particles = [];
    for (var i = 0; i < COUNT; i++) {
      particles.push({ x: Math.random() * canvas.width, y: Math.random() * canvas.height,
        vx: (Math.random() - 0.5) * 0.4, vy: (Math.random() - 0.5) * 0.4,
        r: Math.random() * 1.5 + 0.5, c: colors[Math.floor(Math.random() * colors.length)] });
    }
  }
  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    for (var i = 0; i < particles.length; i++) {
      var p = particles[i];
      p.x += p.vx; p.y += p.vy;
      if (p.x < 0 || p.x > canvas.width) p.vx *= -1;
      if (p.y < 0 || p.y > canvas.height) p.vy *= -1;
      ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(' + p.c + ',.6)'; ctx.fill();
    }
    for (var i = 0; i < particles.length; i++) {
      for (var j = i + 1; j < particles.length; j++) {
        var dx = particles[i].x - particles[j].x, dy = particles[i].y - particles[j].y;
        var d = Math.sqrt(dx * dx + dy * dy);
        if (d < DIST) {
          ctx.beginPath(); ctx.moveTo(particles[i].x, particles[i].y); ctx.lineTo(particles[j].x, particles[j].y);
          ctx.strokeStyle = 'rgba(59,130,246,' + (0.08 * (1 - d / DIST)).toFixed(3) + ')';
          ctx.lineWidth = 0.5; ctx.stroke();
        }
      }
    }
    _customState._animFrame = requestAnimationFrame(draw);
  }
  window.addEventListener('resize', function() { resize(); init(); });
  init(); draw();
}
```

## 背景层渲染

```javascript
export function renderBgLayers(isChapter) {
  return (
    <div>
      <div style={{ position: 'absolute', top: 0, left: 0, width: '100%', height: '100%', pointerEvents: 'none',
        background: 'radial-gradient(ellipse at 20% 30%,rgba(59,130,246,.15) 0%,transparent 50%),' +
                    'radial-gradient(ellipse at 80% 70%,rgba(139,92,246,.12) 0%,transparent 50%),' +
                    'radial-gradient(ellipse at 50% 50%,rgba(11,15,25,.8) 0%,rgba(0,0,0,1) 100%)' }} />
      {isChapter && <div style={{ position: 'absolute', top: 0, left: 0, width: '100%', height: '100%', pointerEvents: 'none',
        backgroundImage: 'linear-gradient(rgba(59,130,246,.03) 1px,transparent 1px),linear-gradient(90deg,rgba(59,130,246,.03) 1px,transparent 1px)',
        backgroundSize: '60px 60px', animation: 'gridMove 20s linear infinite' }} />}
      {isChapter && <div style={{ position: 'absolute', top: 0, left: 0, width: '100%', height: '100%', pointerEvents: 'none',
        background: 'radial-gradient(circle at 50% 50%,rgba(59,130,246,.08) 0%,transparent 70%)',
        animation: 'chapterGlow 6s ease-in-out infinite' }} />}
    </div>
  );
}
```

## dark-tech 主渲染框架

```javascript
export function renderJsx() {
  var self = this;
  var cur = _customState.currentSlide;
  var total = _customState.totalSlides;
  var isFull = _customState.isFullscreen;
  return (
    <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: '#0B0F19',
      overflow: 'hidden', fontFamily: '"Noto Sans SC","PingFang SC","Microsoft YaHei",sans-serif' }}>
      <div style={{ display: 'none' }}>{this.state.timestamp}</div>
      <style dangerouslySetInnerHTML={{ __html: CSS_ANIMATIONS }} />
      <canvas id="ppt-particles" style={{ position: 'fixed', top: 0, left: 0, width: '100vw', height: '100vh',
        zIndex: 1, pointerEvents: 'none', opacity: 0.35 }} />
      <div style={{ position: 'relative', zIndex: 2, width: '100vw', height: '100vh' }}>
        {this.renderSlide1()}
        {this.renderSlide2()}
        {/* 按需添加更多幻灯片 */}
      </div>
      {/* 顶部进度条 */}
      <div style={{ position: 'fixed', top: 0, left: 0, right: 0, height: '3px', zIndex: 101, background: 'rgba(255,255,255,.05)' }}>
        <div style={{ height: '100%', background: 'linear-gradient(90deg,#3b82f6,#a855f7)',
          width: ((cur / total) * 100) + '%', transition: 'width .5s cubic-bezier(.25,.46,.45,.94)', borderRadius: '0 2px 2px 0' }} />
      </div>
      {/* 底部导航 */}
      <div style={{ position: 'fixed', bottom: '30px', left: '50%', transform: 'translateX(-50%)', zIndex: 100,
        display: 'flex', alignItems: 'center', gap: '15px', background: 'rgba(0,0,0,.7)',
        backdropFilter: 'blur(20px)', WebkitBackdropFilter: 'blur(20px)',
        border: '1px solid rgba(255,255,255,.1)', borderRadius: '50px', padding: '10px 25px' }}>
        <button onClick={() => { self.changeSlide(-1); }}
          style={{ background: 'none', border: 'none', color: cur > 1 ? '#fff' : 'rgba(255,255,255,.2)',
            cursor: cur > 1 ? 'pointer' : 'default', fontSize: '18px', padding: '0 5px' }}>◀</button>
        <div style={{ display: 'flex', gap: '6px', alignItems: 'center' }}>
          {Array.from({ length: total }, function(_, i) {
            var isActive = i + 1 === cur;
            var isChap = chapterSlides.indexOf(i + 1) >= 0;
            return <div key={i} onClick={() => { self.goToSlide(i + 1); }}
              style={{ width: isActive ? '24px' : (isChap ? '8px' : '6px'), height: isActive ? '8px' : (isChap ? '8px' : '6px'),
                borderRadius: '4px', background: isActive ? '#3b82f6' : (isChap ? 'rgba(59,130,246,.5)' : 'rgba(255,255,255,.2)'),
                transition: 'all .3s ease', cursor: 'pointer' }} />;
          })}
        </div>
        <button onClick={() => { self.changeSlide(1); }}
          style={{ background: 'none', border: 'none', color: cur < total ? '#fff' : 'rgba(255,255,255,.2)',
            cursor: cur < total ? 'pointer' : 'default', fontSize: '18px', padding: '0 5px' }}>▶</button>
        <div style={{ fontSize: '13px', color: 'rgba(255,255,255,.4)', marginLeft: '5px' }}>{cur}/{total}</div>
      </div>
    </div>
  );
}
```

## dark-tech 注意事项

- 🚨 **禁止 `import`/`require`**：文件顶部不能有任何 import 语句，宜搭沙箱不支持
- **事件绑定必须是真实函数**：可以用 `onClick={handleNext}` 或 `onClick={function() { self.changeSlide(1); }}`；禁止 `onClick={self.changeSlide(1)}` 这种渲染期调用，禁止 JSX 小写 `onclick`（ECharts `graphic.onclick` 不是 JSX 属性，可以保留）
- **禁止 ES6 计算属性名**：`{ [key]: value }` 改为 `var obj = {}; obj[key] = value;`
- **Canvas 初始化延迟**：`setTimeout(() => { self.initParticles(); }, 500)` 确保 DOM 就绪
- **`WebkitBackdropFilter`** 必须与 `backdropFilter` 同时写，兼容 Safari
