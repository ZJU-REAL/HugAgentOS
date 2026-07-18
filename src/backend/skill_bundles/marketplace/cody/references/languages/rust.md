# Rust 语言特定检查规则

## 代码风格

- 使用 `rustfmt` 格式化代码
- 使用 `rust-clippy` 进行 lint 检查
- 变量名使用 `snake_case`
- 函数名使用 `snake_case`
- 结构体和枚举名使用 `PascalCase`
- 常量使用 `UPPER_SNAKE_CASE`

## 常见问题

### 🔴 严重

```rust
// ❌ 使用 unwrap() / expect() 处理可能的错误
let value = map.get("key").unwrap();

// ✅ 使用模式匹配
let value = match map.get("key") {
    Some(v) => v,
    None => return Err(Error::KeyNotFound),
};

// 或使用 if let
if let Some(value) = map.get("key") {
    // 使用 value
}
```

```rust
// ❌ 所有权问题
let s1 = String::from("hello");
let s2 = s1;
// s1 已失效，无法使用

// ✅ 使用引用
let s1 = String::from("hello");
let s2 = &s1;
// s1 仍然可用
```

```rust
// ❌ 线程间传递未实现 Send 的类型
use std::thread;
use std::rc::Rc;

let data = Rc::new(vec![1, 2, 3]);
thread::spawn(move || {
    // Rc 不是线程安全的
});

// ✅ 使用 Arc
use std::sync::Arc;
let data = Arc::new(vec![1, 2, 3]);
let data_clone = Arc::clone(&data);
thread::spawn(move || {
    // Arc 是线程安全的
});
```

### 🟡 建议

```rust
// ❌ 循环中重复分配
let mut result = String::new();
for s in strings {
    result.push_str(&s);
    result.push('\n');
}

// ✅ 预分配容量
let mut result = String::with_capacity(total_len);
for s in strings {
    result.push_str(&s);
    result.push('\n');
}
```

```rust
// ❌ 不必要的克隆
let s1 = String::from("hello");
let s2 = s1.clone();
// 性能开销

// ✅ 使用引用或借用
let s1 = String::from("hello");
let s2 = &s1;
```

### 🟢 优化

```rust
// ❌ 使用 Vec::new() 然后 push
let mut vec = Vec::new();
for i in 0..n {
    vec.push(i);
}

// ✅ 使用迭代器方法
let vec: Vec<i32> = (0..n).collect();
```

```rust
// ❌ 使用索引遍历
for i in 0..items.len() {
    println!("{}: {}", i, items[i]);
}

// ✅ 使用 enumerate
for (i, item) in items.iter().enumerate() {
    println!("{}: {}", i, item);
}
```

## 所有权与借用

- 优先使用借用而非克隆
- 使用 `&mut` 进行可变借用时注意生命周期
- 使用生命周期标注 `'a` 处理引用返回
- 避免 `Rc<T>` / `RefCell<T>` 模式（单线程）

## 错误处理

- 使用 `Result<T, E>` 处理可恢复错误
- 使用 `?` 运算符传播错误
- 使用 `anyhow` 简化应用层错误处理
- 使用 `thiserror` 定义库错误类型

## 并发

- 使用 `Arc` 进行共享所有权
- 使用 `Mutex<T>` 保护共享可变状态
- 使用 `channel` 进行消息传递
- 使用 `tokio` 或 `async-std` 进行异步编程