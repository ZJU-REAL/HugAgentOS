# Python 语言特定检查规则

## 代码风格

- 使用 `is None` / `is not None` 代替 `== None` / `!= None`
- 使用 `with` 语句管理资源（文件、锁、数据库连接）
- 使用 `f-string` 代替 `%` 或 `.format()`（Python 3.6+）
- 避免使用 `from module import *`
- 使用 `snake_case` 命名函数和变量，`PascalCase` 命名类

## 类型注解

- 函数参数和返回值应添加类型注解
- 使用 `Optional[T]` 表示可能为 None 的值
- 使用 `Union[A, B]` 表示多种可能类型
- 使用 `list[T]` / `dict[K, V]` 代替 `List` / `Dict`（Python 3.9+）

## 常见问题

### 🔴 严重

```python
# ❌ 可变默认参数（经典陷阱）
def add_item(item, items=[]):
    items.append(item)
    return items

# ✅ 正确写法
def add_item(item, items=None):
    if items is None:
        items = []
    items.append(item)
    return items
```

```python
# ❌ 使用 eval 处理用户输入
data = eval(user_input)

# ✅ 使用 json 或 ast.literal_eval
import json
data = json.loads(user_input)
```

### 🟡 建议

```python
# ❌ 字符串拼接
result = ""
for s in strings:
    result += s

# ✅ 使用 join
result = "".join(strings)
```

```python
# ❌ 使用列表存储查找
if item in my_list:  # O(n)

# ✅ 使用集合
if item in my_set:  # O(1)
```

### 🟢 优化

```python
# ❌ 手动计数
count = 0
for item in items:
    count += 1

# ✅ 使用 len
count = len(items)
```

```python
# ❌ 使用 range(len())
for i in range(len(items)):
    print(items[i])

# ✅ 直接遍历
for item in items:
    print(item)

# 或需要索引时
for i, item in enumerate(items):
    print(i, item)
```

## 异步代码

- 使用 `asyncio.run()` 作为异步入口
- 使用 `async with` 管理异步资源
- 避免 `asyncio.gather()` 中任务异常被吞掉
- 使用 `anyio` 库实现异步兼容性

## 测试

- 使用 `pytest` 作为测试框架
- 使用 `unittest.mock` 或 `pytest-mock` 进行 mock
- 测试文件命名为 `test_*.py`
- 使用 `@pytest.fixture` 管理测试资源