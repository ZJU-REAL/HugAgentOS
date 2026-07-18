# 图表类型参考：SVG 骨架与最佳实践

本文档提供各类图表的 SVG 骨架模板，供 diagram-builder skill 使用时参考。

---

## 1. 系统架构图（分层架构）

```svg
<svg viewBox="0 0 680 420" xmlns="http://www.w3.org/2000/svg" font-family="sans-serif">
  <!-- 定义箭头 marker -->
  <defs>
    <marker id="arrow" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
      <polygon points="0 0, 10 3.5, 0 7" fill="#555"/>
    </marker>
  </defs>

  <!-- 标题 -->
  <text x="340" y="28" text-anchor="middle" font-size="16" font-weight="bold" fill="#1a1a2e">系统架构图</text>

  <!-- 前端层 -->
  <rect x="40" y="50" width="600" height="70" rx="8" fill="#e8f4f8" stroke="#4a90d9" stroke-width="1.5"/>
  <text x="60" y="75" font-size="12" fill="#555">前端层</text>
  <rect x="80" y="80" width="120" height="30" rx="5" fill="#4a90d9"/>
  <text x="140" y="100" text-anchor="middle" font-size="12" fill="white">Web App</text>
  <rect x="230" y="80" width="120" height="30" rx="5" fill="#4a90d9"/>
  <text x="290" y="100" text-anchor="middle" font-size="12" fill="white">Mobile App</text>

  <!-- 连接箭头 -->
  <line x1="340" y1="120" x2="340" y2="150" stroke="#555" stroke-width="1.5" marker-end="url(#arrow)"/>

  <!-- 后端层 -->
  <rect x="40" y="150" width="600" height="70" rx="8" fill="#f0f7ee" stroke="#5ba55b" stroke-width="1.5"/>
  <text x="60" y="175" font-size="12" fill="#555">后端层</text>
  <rect x="80" y="180" width="120" height="30" rx="5" fill="#5ba55b"/>
  <text x="140" y="200" text-anchor="middle" font-size="12" fill="white">API Gateway</text>
  <rect x="230" y="180" width="120" height="30" rx="5" fill="#5ba55b"/>
  <text x="290" y="200" text-anchor="middle" font-size="12" fill="white">业务服务</text>
  <rect x="380" y="180" width="120" height="30" rx="5" fill="#5ba55b"/>
  <text x="440" y="200" text-anchor="middle" font-size="12" fill="white">认证服务</text>

  <!-- 连接箭头 -->
  <line x1="340" y1="220" x2="340" y2="250" stroke="#555" stroke-width="1.5" marker-end="url(#arrow)"/>

  <!-- 数据层 -->
  <rect x="40" y="250" width="600" height="70" rx="8" fill="#fff3e0" stroke="#e67e22" stroke-width="1.5"/>
  <text x="60" y="275" font-size="12" fill="#555">数据层</text>
  <rect x="80" y="280" width="120" height="30" rx="5" fill="#e67e22"/>
  <text x="140" y="300" text-anchor="middle" font-size="12" fill="white">MySQL</text>
  <rect x="230" y="280" width="120" height="30" rx="5" fill="#e67e22"/>
  <text x="290" y="300" text-anchor="middle" font-size="12" fill="white">Redis</text>
  <rect x="380" y="280" width="120" height="30" rx="5" fill="#e67e22"/>
  <text x="440" y="300" text-anchor="middle" font-size="12" fill="white">Elasticsearch</text>

  <!-- 连接箭头 -->
  <line x1="340" y1="320" x2="340" y2="350" stroke="#555" stroke-width="1.5" marker-end="url(#arrow)"/>

  <!-- 基础设施层 -->
  <rect x="40" y="350" width="600" height="50" rx="8" fill="#f3e5f5" stroke="#9b59b6" stroke-width="1.5"/>
  <text x="60" y="370" font-size="12" fill="#555">基础设施</text>
  <text x="340" y="382" text-anchor="middle" font-size="12" fill="#7d3c98">Kubernetes / Docker / 云平台</text>
</svg>
```

**设计要点**：
- 每层用不同背景色区分（蓝/绿/橙/紫）
- 层间用竖向箭头连接
- 同层组件横向排列，间距均匀

---

## 2. 微服务架构图

**设计要点**：
- 以 API Gateway 为中心
- 各微服务使用圆角矩形，颜色统一
- 消息队列（MQ）用平行四边形或圆柱体表示
- 数据库用圆柱体表示（`ellipse` + 矩形组合）
- 服务间通信用不同颜色箭头区分（同步=实线，异步=虚线）

**SVG 骨架关键元素**：
```svg
<!-- 圆柱形数据库 -->
<ellipse cx="200" cy="360" rx="50" ry="12" fill="#e67e22"/>
<rect x="150" y="360" width="100" height="40" fill="#e67e22"/>
<ellipse cx="200" cy="400" rx="50" ry="12" fill="#d35400"/>
<text x="200" y="385" text-anchor="middle" font-size="11" fill="white">数据库名</text>

<!-- 虚线箭头（异步） -->
<line x1="x1" y1="y1" x2="x2" y2="y2" stroke="#999" stroke-width="1.5"
      stroke-dasharray="6,3" marker-end="url(#arrow)"/>
```

---

## 3. 网络拓扑图

**设计要点**：
- 节点用圆形（`<circle>`）表示网络设备
- 在圆内/圆下放图标或文字
- 连线用灰色实线，标注带宽/协议
- 按网络层次布局（互联网 → DMZ → 内网 → 核心）

**SVG 骨架关键元素**：
```svg
<!-- 网络节点 -->
<circle cx="340" cy="80" r="30" fill="#3498db" stroke="white" stroke-width="2"/>
<text x="340" y="85" text-anchor="middle" font-size="11" fill="white">互联网</text>

<!-- 防火墙（菱形） -->
<polygon points="340,130 370,155 340,180 310,155" fill="#e74c3c"/>
<text x="340" y="160" text-anchor="middle" font-size="10" fill="white">防火墙</text>

<!-- 连线带标注 -->
<line x1="340" y1="110" x2="340" y2="130" stroke="#555" stroke-width="2"/>
<text x="355" y="122" font-size="10" fill="#777">1Gbps</text>
```

---

## 4. 业务流程图（泳道图）

**设计要点**：
- 垂直或水平泳道划分角色
- 开始/结束：圆形（`<circle>`）
- 流程步骤：矩形（`<rect>`）
- 决策：菱形（`<polygon>`）
- 箭头连接，分支标注"是/否"

**SVG 骨架关键元素**：
```svg
<!-- 泳道背景 -->
<rect x="40" y="40" width="190" height="500" fill="#f8f9fa" stroke="#dee2e6"/>
<text x="135" y="65" text-anchor="middle" font-size="13" font-weight="bold" fill="#495057">用户</text>

<!-- 开始节点 -->
<circle cx="135" cy="100" r="20" fill="#2ecc71"/>
<text x="135" y="105" text-anchor="middle" font-size="12" fill="white">开始</text>

<!-- 决策菱形 -->
<polygon points="135,200 175,230 135,260 95,230" fill="#f39c12"/>
<text x="135" y="234" text-anchor="middle" font-size="11" fill="white">审核通过?</text>
<!-- 分支标注 -->
<text x="180" y="225" font-size="10" fill="#555">是</text>
<text x="95" y="255" font-size="10" fill="#555">否</text>
```

---

## 5. 数据流图（DFD）

**设计要点**：
- 外部实体：矩形（方形角）
- 处理过程：圆角矩形或圆形
- 数据存储：两条平行横线加矩形（或圆柱）
- 数据流：带标注的有向箭头

```svg
<!-- 数据存储（两横线样式） -->
<line x1="260" y1="240" x2="420" y2="240" stroke="#555" stroke-width="1.5"/>
<line x1="260" y1="270" x2="420" y2="270" stroke="#555" stroke-width="1.5"/>
<text x="340" y="260" text-anchor="middle" font-size="12" fill="#333">D1: 用户数据库</text>
```

---

## 6. 时序图（Sequence Diagram）

**设计要点**：
- 角色在顶部，用矩形+标签表示
- 生命线为竖向虚线
- 消息为水平箭头，同步用实线，异步用虚线
- 激活框（activation bar）用细高矩形表示

```svg
<!-- 角色 -->
<rect x="60" y="30" width="80" height="30" rx="4" fill="#3498db"/>
<text x="100" y="50" text-anchor="middle" font-size="12" fill="white">客户端</text>
<!-- 生命线 -->
<line x1="100" y1="60" x2="100" y2="450" stroke="#aaa" stroke-width="1" stroke-dasharray="4,3"/>
<!-- 消息箭头 -->
<line x1="100" y1="100" x2="280" y2="100" stroke="#2c3e50" stroke-width="1.5" marker-end="url(#arrow)"/>
<text x="190" y="95" text-anchor="middle" font-size="11" fill="#555">HTTP 请求</text>
```

---

## 7. 状态机图（State Machine）

**设计要点**：
- 状态：圆角矩形（`rx="20"`）
- 初始状态：实心圆（`<circle fill="black">`）
- 终止状态：双圆（内圆 + 外圆）
- 转换：带标注的曲线或直线箭头

```svg
<!-- 状态节点 -->
<rect x="120" y="80" width="100" height="40" rx="20" fill="#3498db" stroke="#2980b9" stroke-width="1.5"/>
<text x="170" y="105" text-anchor="middle" font-size="13" fill="white">待支付</text>

<!-- 初始状态 -->
<circle cx="170" cy="50" r="10" fill="#2c3e50"/>
<line x1="170" y1="60" x2="170" y2="80" stroke="#2c3e50" stroke-width="1.5" marker-end="url(#arrow)"/>

<!-- 终止状态 -->
<circle cx="170" cy="400" r="14" fill="none" stroke="#2c3e50" stroke-width="2"/>
<circle cx="170" cy="400" r="9" fill="#2c3e50"/>
```

---

## 8. ER 图（实体关系图）

**设计要点**：
- 实体：矩形，标题栏 + 属性列表
- 主键：加粗或下划线
- 关系线：端点用菱形/竖线/叉表示基数（1:1 / 1:N / M:N）
- 属性较多时可折叠展示部分

```svg
<!-- 实体 -->
<rect x="60" y="80" width="160" height="140" rx="6" fill="white" stroke="#3498db" stroke-width="2"/>
<rect x="60" y="80" width="160" height="32" rx="6" fill="#3498db"/>
<!-- 底部圆角修正 -->
<rect x="60" y="96" width="160" height="16" fill="#3498db"/>
<text x="140" y="101" text-anchor="middle" font-size="13" font-weight="bold" fill="white">User</text>
<!-- 属性列 -->
<line x1="60" y1="112" x2="220" y2="112" stroke="#3498db" stroke-width="1"/>
<text x="75" y="132" font-size="12" fill="#333" font-weight="bold">🔑 id</text>
<text x="75" y="152" font-size="12" fill="#555">name</text>
<text x="75" y="172" font-size="12" fill="#555">email</text>
<text x="75" y="192" font-size="12" fill="#555">created_at</text>
```

---

## 通用设计参数

| 参数 | 推荐值 |
|------|--------|
| SVG 宽度 | 固定 680px |
| SVG 高度 | 按内容计算，常见 300~600px |
| 节点圆角 | `rx="6"` ~ `rx="12"` |
| 主字体 | `font-family="sans-serif"` |
| 标题字号 | 14~16px，`font-weight="bold"` |
| 正文字号 | 11~13px |
| 节点间距 | 最少 16px |
| 文字内边距 | 最少 8px |
| 箭头颜色 | `#555`（默认）、`#3498db`（主色）、`#e74c3c`（警示） |
| 分层背景 | `#e8f4f8`（蓝）、`#f0f7ee`（绿）、`#fff3e0`（橙）、`#f3e5f5`（紫） |
