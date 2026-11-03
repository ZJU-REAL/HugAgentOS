# discover — 发现推荐好书

## 接口

### `/book/recommend` — 个性化推荐（为你推荐）

基于用户阅读记录的个性化推荐，与 App 首页「为你推荐」一致。

**请求参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `count` | int | 否 | 每页数量，默认 12 |
| `maxIdx` | int | 否 | 翻页偏移，默认 0 |

**回包：**

| 字段 | 说明 |
|------|------|
| `books` | 推荐书籍数组 |
| `books[].bookId` | 书籍 ID |
| `books[].readerLink` | 阅读器跳转链接；展示为 `[打开阅读]({readerLink})` |
| `books[].deepLink` | 书籍详情跳转链接；没有 `readerLink` 时可展示为 `[查看详情]({deepLink})` |
| `books[].title` | 书名 |
| `books[].author` | 作者 |
| `books[].cover` | 封面图 URL |
| `books[].intro` | 简介 |
| `books[].category` | 分类 |
| `books[].reason` | 推荐理由 |
| `books[].readingCount` | 在读人数 |
| `books[].searchIdx` | 结果序号（用于翻页） |
| `books[].newRating` | 评分（0-100） |
| `books[].newRatingCount` | 评分人数 |
| `books[].newRatingDetail.title` | 评分标签（如"神作""力荐"） |
| `books[].price` | 价格（分） |
| `books[].payType` | 付费类型 |
| `books[].type` | 书籍类型（0=电子书） |

### `/book/similar` — 相似书推荐

基于某本书推荐相似书籍，与 App 书籍详情页「相似推荐」一致。

**请求参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `bookId` | string | 是 | 书籍 ID |
| `count` | int | 是 | 每页数量；实际调用必须显式传入，首次推荐传 12，用户明确指定数量时可传对应数量 |
| `maxIdx` | int | 是 | 翻页偏移；实际调用必须显式传入，首次传 0，翻页传上一页最后一条的 `idx` |
| `sessionId` | string | 否 | 翻页会话 ID（首次不传，后续传回包中的值） |

> `/book/similar` 实际转发到 `/book/detailinfo?listtypes=2&synckey=0`。底层要求 `listTypes`、`synckey`、`maxIdx`、`count` 四组参数长度一致，因此调用时不能省略 `count` 和 `maxIdx`，不要依赖接口文档中的默认值。

**回包：**

| 字段 | 说明 |
|------|------|
| `booksimilar.sessionId` | 会话 ID（翻页时传入下次请求） |
| `booksimilar.books` | 推荐书籍数组 |
| `booksimilar.books[].idx` | 结果序号（下次请求 maxIdx 传最后一条的 idx） |
| `booksimilar.books[].book.bookInfo` | 书籍信息（bookId, title, author, cover 等） |
| `booksimilar.books[].book.bookInfo.readerLink` | 阅读器跳转链接；展示为 `[打开阅读]({readerLink})` |
| `booksimilar.books[].book.bookInfo.deepLink` | 书籍详情跳转链接；没有 `readerLink` 时可展示为 `[查看详情]({deepLink})` |

## 工作流

1. **无参数**：调 `/book/recommend` 获取个性化推荐（为你推荐）。
2. **有 bookId**：调 `/book/similar` 推荐相似书，必须显式传 `count` 和 `maxIdx`；首次推荐 `count=12`、`maxIdx=0`。
3. **有关键词**：调 `/store/search` 搜索发现。
4. 用户对推荐的书感兴趣时，调 `/book/info` 获取完整信息。
5. 翻页（recommend）：用 `searchIdx` 作为下次的 `maxIdx`。
6. 翻页（similar）：用最后一条的 `idx` 作为 `maxIdx`，继续显式传 `count`，带上 `sessionId`。

## 输出格式
- 推荐列表用编号展示，每本书含书名、作者、评分、推荐理由
- 提示用户可选择编号查看详情或继续推荐更多
- 无推荐结果时回复：`暂时没有找到合适的推荐，换个关键词试试？`
- 接口调用失败时回复：`推荐数据获取失败，请稍后再试~`
