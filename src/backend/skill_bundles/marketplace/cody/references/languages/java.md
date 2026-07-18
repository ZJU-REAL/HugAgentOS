# Java 语言特定检查规则

## 代码风格

- 类名使用 `PascalCase`
- 方法名和变量名使用 `camelCase`
- 常量使用 `UPPER_SNAKE_CASE`
- 使用 `final` 标记不可变变量

## 常见问题

### 🔴 严重

```java
// ❌ 空指针未检查
String name = user.getName().toUpperCase();

// ✅ 空值检查
String name = user.getName();
if (name != null) {
    name = name.toUpperCase();
}

// 或使用 Optional
String name = Optional.ofNullable(user.getName())
    .map(String::toUpperCase)
    .orElse("");
```

```java
// ❌ 资源未关闭
FileInputStream fis = new FileInputStream(file);
// 使用 fis...
// 未关闭

// ✅ 使用 try-with-resources
try (FileInputStream fis = new FileInputStream(file)) {
    // 使用 fis...
}  // 自动关闭
```

### 🟡 建议

```java
// ❌ 使用可变对象作为集合元素
List<List<Integer>> lists = new ArrayList<>();
List<Integer> list = new ArrayList<>();
for (int i = 0; i < 3; i++) {
    list.add(i);
    lists.add(list);  // 添加同一个对象引用
}

// ✅ 每次创建新对象
for (int i = 0; i < 3; i++) {
    List<Integer> list = new ArrayList<>();
    list.add(i);
    lists.add(list);
}
```

```java
// ❌ 字符串拼接
String result = "";
for (String s : strings) {
    result += s;
}

// ✅ 使用 StringBuilder
StringBuilder sb = new StringBuilder();
for (String s : strings) {
    sb.append(s);
}
String result = sb.toString();
```

### 🟢 优化

```java
// ❌ 使用 Vector / Hashtable（已过时）
List<String> list = new Vector<>();

// ✅ 使用 ArrayList / HashMap
List<String> list = new ArrayList<>();
```

## 异常处理

- 不要捕获 `Exception` 或 `Throwable`，捕获具体异常
- 不要空 catch 块
- 使用 try-with-resources 管理 AutoCloseable 资源
- 区分 checked 和 unchecked 异常的使用场景

## 并发

- 使用 `java.util.concurrent` 包
- 使用 `ConcurrentHashMap` 代替 `synchronized HashMap`
- 使用线程池 `ExecutorService` 代替直接创建线程
- 使用 `CompletableFuture` 进行异步编程