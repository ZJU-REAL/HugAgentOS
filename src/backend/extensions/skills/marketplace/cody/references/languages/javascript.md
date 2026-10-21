# JavaScript / TypeScript 语言特定检查规则

## 代码风格

- 使用 `const` / `let` 代替 `var`
- 使用箭头函数 `() => {}` 保持 `this` 绑定
- 使用模板字符串 `` `${var}` `` 代替字符串拼接
- 使用解构赋值 `const { name } = obj`

## TypeScript 特定

- 避免使用 `any` 类型，使用 `unknown` + 类型守卫
- 使用严格模式 `strict: true`（tsconfig.json）
- 使用 `interface` 定义对象形状，`type` 定义联合/交叉类型
- 使用 `as const` 定义字面量类型

## 常见问题

### 🔴 严重

```javascript
// ❌ 使用 == 而非 ===
if (value == "true")  // 0 == "0" 为 true

// ✅ 使用严格相等
if (value === "true")
```

```javascript
// ❌ 数组/对象作为默认参数（与 Python 类似）
function addItem(item, items = []) {
  // 但这其实在 JS 中是安全的，因为每次调用都会创建新数组
}
```

```javascript
// ❌ 未处理 Promise 异常
fetch(url).then(res => res.json())

// ✅ 添加错误处理
fetch(url)
  .then(res => res.json())
  .catch(err => console.error(err))

// 或使用 async/await
try {
  const res = await fetch(url)
  const data = await res.json()
} catch (err) {
  console.error(err)
}
```

### 🟡 建议

```javascript
// ❌ 回调地狱
getData(a => {
  getMore(a, b => {
    getMore(b, c => {
      // ...
    })
  })
})

// ✅ 使用 Promise 链或 async/await
const a = await getData()
const b = await getMore(a)
const c = await getMore(b)
```

```javascript
// ❌ 在循环中使用 await
for (const url of urls) {
  await fetch(url)  // 串行执行
}

// ✅ 并行执行
await Promise.all(urls.map(url => fetch(url)))
```

### 🟢 优化

```javascript
// ❌ 使用 for...in 遍历数组
for (const i in arr) {
  console.log(arr[i])
}

// ✅ 使用 for...of
for (const item of arr) {
  console.log(item)
}
```

```javascript
// ❌ 手动拼接数组
const result = []
for (const item of items) {
  result.push(transform(item))
}

// ✅ 使用 map
const result = items.map(transform)
```

## React 特定

- 使用函数组件 + Hooks
- 使用 `useCallback` / `useMemo` 优化性能
- 避免 `useEffect` 依赖数组遗漏
- 使用 `React.memo` 防止不必要的重渲染

## Node.js 特定

- 使用 `fs.promises` 代替 `fs` 回调版本
- 使用 `path.join()` 处理路径
- 使用 `dotenv` 管理环境变量
- 使用 `express-async-errors` 处理异步错误