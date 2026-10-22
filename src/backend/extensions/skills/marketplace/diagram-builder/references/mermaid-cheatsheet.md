# Mermaid 语法速查与模板

本文件提供 Mermaid 图表语法速查和常用模板，供 diagram-builder skill 使用。

---

## 流程图（Flowchart）

```mermaid
flowchart TD
    A([开始]) --> B[步骤一]
    B --> C{判断条件}
    C -->|是| D[步骤二A]
    C -->|否| E[步骤二B]
    D --> F([结束])
    E --> F

    %% 样式
    style A fill:#2ecc71,color:white
    style F fill:#e74c3c,color:white
    style C fill:#f39c12,color:white
```

**节点形状**：
| 语法 | 形状 |
|------|------|
| `A[文字]` | 矩形 |
| `A(文字)` | 圆角矩形 |
| `A([文字])` | 胶囊形（开始/结束） |
| `A{文字}` | 菱形（判断） |
| `A[(文字)]` | 圆柱（数据库） |
| `A((文字))` | 圆形 |
| `A>文字]` | 标签形 |

**方向**：`TD`（上下）、`LR`（左右）、`BT`（下上）、`RL`（右左）

---

## 时序图（Sequence Diagram）

```mermaid
sequenceDiagram
    autonumber
    participant C as 客户端
    participant G as API Gateway
    participant S as 业务服务
    participant D as 数据库

    C->>G: POST /api/login
    G->>S: 验证请求
    S->>D: 查询用户
    D-->>S: 返回用户数据
    S->>S: 校验密码
    alt 登录成功
        S-->>G: 返回 Token
        G-->>C: 200 OK {token}
    else 登录失败
        S-->>G: 认证失败
        G-->>C: 401 Unauthorized
    end
```

**消息类型**：
- `A->>B: 消息` — 实线箭头（同步）
- `A-->>B: 消息` — 虚线箭头（返回/异步）
- `A-)B: 消息` — 异步（无箭头实线）
- `A-xB: 消息` — 带 × 的箭头（失败）

**控制块**：
```
loop 每10秒轮询
    ...
end

alt 条件A
    ...
else 条件B
    ...
end

opt 可选操作
    ...
end

par 并行操作1
    ...
and 并行操作2
    ...
end
```

---

## 状态机（State Diagram v2）

```mermaid
stateDiagram-v2
    [*] --> 待支付
    待支付 --> 已支付 : 用户支付
    待支付 --> 已取消 : 超时/主动取消
    已支付 --> 发货中 : 商家发货
    发货中 --> 已完成 : 确认收货
    发货中 --> 退款中 : 申请退款
    退款中 --> 已退款 : 退款成功
    已完成 --> [*]
    已取消 --> [*]
    已退款 --> [*]

    note right of 发货中
        物流跟踪中
    end note
```

---

## ER 图（Entity Relationship）

```mermaid
erDiagram
    USER {
        int id PK
        string name
        string email
        datetime created_at
    }
    ORDER {
        int id PK
        int user_id FK
        decimal total_amount
        string status
        datetime order_time
    }
    ORDER_ITEM {
        int id PK
        int order_id FK
        int product_id FK
        int quantity
        decimal unit_price
    }
    PRODUCT {
        int id PK
        string name
        decimal price
        int stock
    }

    USER ||--o{ ORDER : "下单"
    ORDER ||--|{ ORDER_ITEM : "包含"
    PRODUCT ||--o{ ORDER_ITEM : "被订购"
```

**基数符号**：
| 符号 | 含义 |
|------|------|
| `\|\|` | 有且仅有一个 |
| `o\|` | 零或一个 |
| `\|{` | 一或多个 |
| `o{` | 零或多个 |

---

## 类图（Class Diagram）

```mermaid
classDiagram
    class Animal {
        +String name
        +int age
        +makeSound() void
    }
    class Dog {
        +String breed
        +fetch() void
    }
    class Cat {
        +bool indoor
        +purr() void
    }
    Animal <|-- Dog : 继承
    Animal <|-- Cat : 继承
    Dog "1" --> "0..*" Owner : 属于

    class Owner {
        +String name
        +adoptPet(animal: Animal) void
    }
```

---

## 甘特图（Gantt）

```mermaid
gantt
    title 项目开发计划
    dateFormat YYYY-MM-DD
    section 需求阶段
    需求调研     :a1, 2026-05-01, 7d
    需求评审     :a2, after a1, 3d
    section 开发阶段
    后端开发     :b1, after a2, 14d
    前端开发     :b2, after a2, 14d
    section 测试阶段
    集成测试     :c1, after b1, 7d
    UAT 测试     :c2, after c1, 5d
    section 发布
    上线部署     :d1, after c2, 2d
```

---

## 饼图（Pie Chart）

```mermaid
pie title 技术栈分布
    "后端" : 35
    "前端" : 30
    "DevOps" : 20
    "数据" : 15
```

---

## 思维导图（Mindmap）

```mermaid
mindmap
  root((系统架构))
    前端
      Web App
        React
        Vue
      Mobile App
        iOS
        Android
    后端
      API Gateway
      微服务
        用户服务
        订单服务
        支付服务
    数据层
      MySQL
      Redis
      Elasticsearch
    基础设施
      Kubernetes
      CI/CD
      监控告警
```

---

## 常用技巧

### 添加链接
```
A --> B
click A "https://example.com" "跳转说明"
```

### 子图（Subgraph）
```mermaid
flowchart LR
    subgraph 前端
        A[Web] & B[Mobile]
    end
    subgraph 后端
        C[API] --> D[服务]
    end
    A & B --> C
```

### 自定义样式
```
style nodeId fill:#color,stroke:#color,color:#color
classDef myClass fill:#f9f,stroke:#333
class nodeId myClass
```

### 图表配置
```
%%{init: {'theme': 'base', 'themeVariables': {'primaryColor': '#3498db'}}}%%
```

可用主题：`base`、`default`、`dark`、`forest`、`neutral`
