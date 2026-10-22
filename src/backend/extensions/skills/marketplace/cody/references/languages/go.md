# Go 语言特定检查规则

## 代码风格

- 使用 `gofmt` 格式化代码
- 使用 `go vet` 静态分析
- 使用 `golangci-lint` 综合检查
- 错误处理：`if err != nil` 模式

## 常见问题

### 🔴 严重

```go
// ❌ 忽略错误返回值
result, _ := doSomething()

// ✅ 处理错误
result, err := doSomething()
if err != nil {
    return err
}
```

```go
// ❌ goroutine 泄漏
go func() {
    for {
        // 无退出条件
    }
}()

// ✅ 使用 context 控制退出
ctx, cancel := context.WithCancel(context.Background())
go func() {
    for {
        select {
        case <-ctx.Done():
            return
        default:
            // work
        }
    }
}()
```

```go
// ❌ 循环变量闭包问题
for _, v := range items {
    go func() {
        process(v)  // 所有 goroutine 使用同一个 v
    }()
}

// ✅ 传递副本
for _, v := range items {
    v := v  // 创建副本
    go func() {
        process(v)
    }()
}
```

### 🟡 建议

```go
// ❌ 使用 panic 处理业务错误
if err != nil {
    panic(err)
}

// ✅ 返回错误
if err != nil {
    return err
}
```

```go
// ❌ 未使用 defer 释放资源
file, err := os.Open(path)
if err != nil {
    return err
}
// ... 中间可能 return，file 未关闭

// ✅ 使用 defer
file, err := os.Open(path)
if err != nil {
    return err
}
defer file.Close()
```

### 🟢 优化

```go
// ❌ 使用 append 时未预分配
var result []int
for _, v := range items {
    result = append(result, v)
}

// ✅ 预分配容量
result := make([]int, 0, len(items))
for _, v := range items {
    result = append(result, v)
}
```

```go
// ❌ 字符串拼接
var result string
for _, s := range strings {
    result += s
}

// ✅ 使用 strings.Builder
var builder strings.Builder
for _, s := range strings {
    builder.WriteString(s)
}
result := builder.String()
```

## 并发安全

- 使用 `sync.Mutex` 保护共享资源
- 使用 `sync.RWMutex` 区分读写锁
- 使用 `sync.WaitGroup` 等待 goroutine 完成
- 使用 channel 进行 goroutine 通信
- 避免 data race（使用 `-race` 检测）

## 错误处理

- 使用 `errors.New()` 创建简单错误
- 使用 `fmt.Errorf()` 格式化错误
- 使用 `errors.Is()` / `errors.As()` 判断错误类型
- 使用自定义错误类型提供更多上下文