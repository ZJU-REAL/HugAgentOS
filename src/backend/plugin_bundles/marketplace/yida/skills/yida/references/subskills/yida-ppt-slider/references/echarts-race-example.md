# ECharts Bar Chart Race 动态柱状图示例

> 完整的 Bar Chart Race 实现代码，从 `yida-ppt-slider` 主技能提取。
> 用于在 PPT 幻灯片中集成 ECharts 动态柱状图，展示中国历代经济排名变化。

## SLIDES 数据结构

```javascript
// 在 SLIDES 数组中新增 echarts-race 类型幻灯片
{
  type: 'echarts-race',
  bg: '#ffffff',
  accent: '#d97706',
  title: '中国历代经济排名变化',
  subtitle: '公元前 2000 年 - 公元 2025 年',
}
```

## RACE_DATA 数据定义

```javascript
var RACE_DATA = {
  // 8 个实体的名称和颜色
  entities: [
    { name: '中国', color: '#ff4444' },      // 固定红色
    { name: '印度', color: '#3b82f6' },
    { name: '欧洲', color: '#10b981' },
    { name: '中东', color: '#f59e0b' },
    { name: '美国', color: '#8b5cf6' },
    { name: '日本', color: '#ec4899' },
    { name: '俄罗斯', color: '#6366f1' },
    { name: '其他', color: '#9ca3af' },
  ],
  // 中国朝代名称映射（随时间动态切换）
  chinaNames: {
    '-2000': '华夏',
    '-770': '春秋列国',
    '-221': '大秦',
    '-206': '大汉',
    '581': '大隋',
    '618': '大唐',
    '960': '北宋',
    '1127': '南宋',
    '1368': '大明',
    '1644': '大清',
    '1912': '中华民国',
    '1949': '新中国',
    '2025': '中国',
  },
  // 历史数据（年份 + 8 个实体的 GDP 数值）
  timeline: [
    { year: -2000, values: [120, 80, 40, 30, 10, 5, 5, 20] },
    { year: -770, values: [150, 90, 50, 40, 10, 8, 8, 25] },
    { year: -221, values: [200, 100, 60, 50, 15, 10, 10, 30] },
    { year: -206, values: [300, 120, 80, 60, 20, 15, 15, 40] },
    { year: 581, values: [400, 150, 100, 80, 30, 20, 20, 50] },
    { year: 618, values: [500, 180, 120, 100, 40, 25, 25, 60] },
    { year: 960, values: [600, 200, 150, 120, 50, 30, 30, 70] },
    { year: 1127, values: [550, 220, 180, 140, 60, 35, 35, 80] },
    { year: 1368, values: [700, 250, 200, 160, 80, 40, 40, 90] },
    { year: 1644, values: [800, 280, 250, 180, 100, 50, 50, 100] },
    { year: 1912, values: [600, 300, 300, 200, 150, 60, 60, 120] },
    { year: 1949, values: [500, 320, 350, 220, 200, 80, 80, 150] },
    { year: 2025, values: [18000, 3500, 20000, 3000, 25000, 4000, 1800, 5000] },
  ],
};
```

## renderSlideContent 中处理 echarts-race 类型

```javascript
export function renderSlideContent(slide, accent, isMobile) {
  if (slide.type === 'echarts-race') {
    return (
      <div style={{
        width: '100%',
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
      }}>
        {/* 标题区 */}
        <div style={{ textAlign: 'center', marginBottom: '20px' }}>
          <h2 style={{ fontSize: isMobile ? '20px' : '28px', color: '#1a1a2e', margin: 0 }}>
            {slide.title}
          </h2>
          {slide.subtitle && (
            <p style={{ fontSize: isMobile ? '14px' : '18px', color: 'rgba(26,26,46,0.7)', marginTop: '8px' }}>
              {slide.subtitle}
            </p>
          )}
        </div>

        {/* ECharts 容器 */}
        <div
          id="echarts-race-container"
          style={{
            flex: 1,
            width: '100%',
            minHeight: '400px',
            position: 'relative',
          }}
        />

        {/* 加载 ECharts 脚本 */}
        <script
          src="https://g.alicdn.com/code/lib/echarts/5.6.0/echarts.min.js"
          onLoad={function() {
            self.initBarChartRace();
          }}
        />
      </div>
    );
  }
  // ... 其他类型处理
}
```

## initBarChartRace 初始化函数

```javascript
export function initBarChartRace() {
  var self = this;
  var container = document.getElementById('echarts-race-container');
  if (!container) return;

  var chart = echarts.init(container);
  var entities = RACE_DATA.entities;
  var timeline = RACE_DATA.timeline;
  var chinaNames = RACE_DATA.chinaNames;

  var currentIndex = 0;
  var isPlaying = true;
  var timer = null;

  // 线性插值函数（在两个时间点之间生成中间帧）
  var lerp = function(start, end, t) {
    return start + (end - start) * t;
  };

  // 生成插值帧（每 50ms 一帧，实现平滑动画）
  var framesPerYear = 20;  // 每年 20 帧
  var allFrames = [];

  for (var i = 0; i < timeline.length - 1; i++) {
    var currentYear = timeline[i];
    var nextYear = timeline[i + 1];
    var yearDiff = nextYear.year - currentYear.year;
    var totalFrames = yearDiff * framesPerYear;

    for (var f = 0; f < totalFrames; f++) {
      var t = f / totalFrames;
      var frameYear = currentYear.year + yearDiff * t;
      var frameValues = [];

      for (var j = 0; j < 8; j++) {
        frameValues.push(lerp(currentYear.values[j], nextYear.values[j], t));
      }

      allFrames.push({
        year: Math.round(frameYear),
        values: frameValues,
      });
    }
  }
  // 添加最后一个时间点
  allFrames.push({
    year: timeline[timeline.length - 1].year,
    values: timeline[timeline.length - 1].values,
  });

  // 获取中国朝代名
  var getChinaName = function(year) {
    var names = [];
    for (var key in chinaNames) {
      var keyYear = parseInt(key, 10);
      if (year >= keyYear) {
        names.push({ year: keyYear, name: chinaNames[key] });
      }
    }
    return names.length > 0 ? names[names.length - 1].name : '中国';
  };

  // 更新图表
  var updateChart = function() {
    var frame = allFrames[currentIndex];
    var year = frame.year;
    var values = frame.values;

    // 构造数据（按值排序）
    var data = entities.map(function(entity, index) {
      return {
        name: entity.name === '中国' ? getChinaName(year) : entity.name,
        value: values[index],
        itemStyle: { color: entity.color },
      };
    });
    data = _.orderBy(data, 'value', 'desc');

    var option = {
      grid: { top: '10%', right: '15%', bottom: '15%', left: '15%' },
      xAxis: { show: false },
      yAxis: {
        type: 'category',
        data: data.map(function(d) { return d.name; }),
        axisLabel: { fontSize: 14 },
      },
      series: [{
        type: 'bar',
        data: data.map(function(d) { return d.value; }),
        itemStyle: {
          borderRadius: [0, 4, 4, 0],
        },
        label: {
          show: true,
          position: 'right',
          formatter: function(params) {
            return params.value.toLocaleString();
          },
        },
        barWidth: '60%',
      }],
      // 底部时间轴进度条
      graphic: [
        // 时间轴背景
        {
          type: 'rect',
          left: '5%',
          right: '5%',
          top: '85%',
          height: 4,
          shape: { fill: '#e5e7eb' },
        },
        // 时间轴进度
        {
          type: 'rect',
          left: '5%',
          top: '85%',
          height: 4,
          shape: { fill: '#d97706' },
          style: {
            width: ((currentIndex / (allFrames.length - 1)) * 90) + '%',
          },
        },
        // 右下角大字年份水印
        {
          type: 'text',
          right: '5%',
          bottom: '10%',
          style: {
            text: year < 0 ? Math.abs(year) + ' BC' : year + ' AD',
            fontSize: 48,
            fontWeight: 'bold',
            fill: 'rgba(26,26,46,0.1)',
          },
        },
        // 播放完毕显示重播按钮
        currentIndex === allFrames.length - 1 ? {
          type: 'text',
          left: 'center',
          top: 'center',
          style: {
            text: '🔄 重播',
            fontSize: 24,
            fill: '#d97706',
            cursor: 'pointer',
          },
          onclick: function() {
            currentIndex = 0;
            isPlaying = true;
            play();
          },
        } : null,
      ].filter(Boolean),
    };

    chart.setOption(option);
  };

  // 播放动画
  var play = function() {
    if (timer) clearInterval(timer);
    timer = setInterval(function() {
      if (currentIndex < allFrames.length - 1) {
        currentIndex++;
        updateChart();
      } else {
        isPlaying = false;
        clearInterval(timer);
      }
    }, 50);  // 每 50ms 一帧
  };

  // 初始化并开始播放
  updateChart();
  play();

  // 组件卸载时清理
  this._chartRaceCleanup = function() {
    if (timer) clearInterval(timer);
    if (chart) chart.dispose();
  };
}

// 在 didUnmount 中清理 ECharts 实例
export function didUnmount() {
  // ... 其他清理
  if (this._chartRaceCleanup) {
    this._chartRaceCleanup();
  }
}
```

## 实现要点

- **ECharts 加载**：通过阿里 CDN 加载 ECharts 5.6.0（`https://g.alicdn.com/code/lib/echarts/5.6.0/echarts.min.js`）
- **新增 slide 类型**：`echarts-race`，包含 `title` 和 `subtitle` 字段
- **8 个实体**：中国、印度、欧洲、中东、美国、日本、俄罗斯、其他，中国固定红色 `#ff4444`
- **中国朝代名动态切换**：根据年份自动切换朝代名称（华夏→春秋列国→大秦→大汉→大隋→大唐→北宋→南宋→大明→大清→中华民国→新中国→中国）
- **逐帧线性插值**：在两个时间点之间生成中间帧，实现平滑动画过渡
- **底部时间轴进度条**：显示播放进度，使用 ECharts `graphic` 组件绘制
- **右下角大字年份水印**：显示当前年份，负数年份显示 "BC"（公元前）
- **播放完毕重播按钮**：使用 ECharts `graphic onclick` 事件，点击后重置并重新播放
- **连续播放无停顿**：使用 `setInterval` 每 50ms 更新一帧，确保动画流畅
- **内存清理**：在 `didUnmount` 中清理定时器和 ECharts 实例，防止内存泄漏

## 数据结构说明

- `RACE_DATA.entities`：8 个实体的名称和颜色配置
- `RACE_DATA.chinaNames`：中国朝代名称映射表（年份 → 朝代名）
- `RACE_DATA.timeline`：历史时间线数据，每个时间点包含年份和 8 个实体的 GDP 数值
