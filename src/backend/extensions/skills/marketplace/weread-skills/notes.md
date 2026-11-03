# notes — 笔记/划线

本文档区分两种口径：

- **统计口径**：笔记数 = 书签数 + 划线数 + 想法/点评数。这里的"想法/点评"对应后端 `reviewCount`，包含划线想法、书评想法/个人点评、书摘、非书籍想法等个人内容。
- **内容导出口径**：当前可导出的单本书笔记内容 = 划线内容 + 想法/点评内容。书签只在统计数量中体现，当前 `/book/bookmarklist` 已过滤书签，不能导出书签内容。

公开的他人点评不属于个人笔记，见 `review.md`。

## 接口

### `/user/notebooks` — 笔记本概览（所有有笔记的书）

**请求参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `count` | int | 否 | 每页数量，默认 20 |
| `lastSort` | int | 否 | 翻页游标（上一页最后一条的 `sort` 值） |

**回包：**

| 字段 | 说明 |
|------|------|
| `totalBookCount` | 有笔记的书籍总数 |
| `totalNoteCount` | 笔记总条数，统计口径为 `reviewCount + noteCount + bookmarkCount` 的汇总 |
| `hasMore` | 是否有更多（1=有） |
| `books[].bookId` | 书籍 ID |
| `books[].book` | 书籍信息（title, author, cover 等） |
| `books[].reviewCount` | 想法/点评数：包含划线想法、书评想法/个人点评、书摘、非书籍想法等个人内容 |
| `books[].noteCount` | 划线数（高亮标注的原文条数） |
| `books[].bookmarkCount` | 书签数（标记阅读位置的条数；只作为数量统计，当前不导出书签内容） |
| `books[].readingProgress` | 阅读进度 |
| `books[].markedStatus` | 标记状态（1=读完, 0=在读） |
| `books[].sort` | 排序值（最近笔记时间，用于翻页） |

#### 概念解释
- 用户问“有多少笔记”时，使用统计口径：`reviewCount + noteCount + bookmarkCount`。
- `noteCount` 字段名容易误读：它不是单本书总笔记数，而是划线/高亮原文条数；单本书总笔记数必须自行计算。
- `/user/notebooks` 不返回 `highlightCount` 字段；如果用户或上游说“高亮数/划线数”，对应字段是 `noteCount`。
- `reviewCount` 已包含个人点评/书评想法，因此计算总笔记数时不要再额外加“点评数”，否则会重复计算。
- `/user/notebooks` 概览无法把 `reviewCount` 拆成“划线想法”和“个人点评”的独立数量；如需内容明细，需继续查询 `/review/list/mine`。

#### 分页规则
- `/user/notebooks` 使用基于时间排序值的游标分页，不支持 `offset`/`limit` 分页。
- 第一次请求只传 `count`；如果 `hasMore` 为 1，取本页 `books` 最后一项的 `sort`，下一次作为 `lastSort` 传入。
- 所有业务参数必须平铺在 JSON body 顶层，和 `api_name`、`skill_version` 同级；不要包在 `params` 对象里。
- 不要传 `offset`、`limit`、`start`、`size`；这些参数不会被后端分页逻辑读取，可能导致重复第一页或结果不符合预期。
- 拉取完整列表时循环请求直到 `hasMore` 为 0，再按 `reviewCount + noteCount + bookmarkCount` 计算并降序排序。

#### 分页 few-shot

正确：首页请求，参数平铺。
```json
{"api_name":"/user/notebooks","count":20,"skill_version":"1.0.5"}
```

正确：下一页请求，`lastSort` 取上一页 `books` 最后一项的 `sort`。
```json
{"api_name":"/user/notebooks","count":20,"lastSort":1778312777,"skill_version":"1.0.5"}
```

错误：不要使用 `params` 包裹业务参数，否则后端收不到 `count` 和 `lastSort`。
```json
{"api_name":"/user/notebooks","params":{"count":20,"lastSort":1778312777},"skill_version":"1.0.5"}
```

错误：不要使用 `offset`/`limit`，这些字段不是本接口分页参数。
```json
{"api_name":"/user/notebooks","offset":20,"limit":20,"skill_version":"1.0.5"}
```

### `/book/bookmarklist` — 单本书的划线内容列表（不含书签内容）

> 自动过滤书签（type=0），只返回划线（type=1）。

**请求参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `bookId` | string | 是 | 书籍 ID |

**回包：**

| 字段 | 说明 |
|------|------|
| `updated` | 划线数组 |
| `updated[].bookmarkId` | 划线唯一 ID |
| `updated[].bookId` | 书籍 ID |
| `updated[].chapterUid` | 所在章节 UID |
| `updated[].markText` | 划线原文 |
| `updated[].createTime` | 创建时间（Unix 时间戳） |
| `updated[].type` | 类型 |
| `updated[].range` | 位置范围 |
| `updated[].colorStyle` | 划线颜色样式 |
| `chapters` | 章节信息数组（用于定位划线所属章节） |
| `chapters[].chapterUid` | 章节 UID |
| `chapters[].chapterIdx` | 章节序号 |
| `chapters[].title` | 章节标题 |
| `book` | 书籍信息 |

### `/review/list/mine` — 单本书的个人想法与点评

> 返回当前用户在该书上的所有个人内容，包括划线想法、章节点评和整本书评。

**请求参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `bookid` | string | 是 | 书籍 ID |
| `synckey` | int | 否 | 翻页游标，默认 0 |
| `count` | int | 否 | 每页数量，默认 20 |

**回包：**

| 字段 | 说明 |
|------|------|
| `reviews` | 想法/点评数组 |
| `reviews[].review.reviewId` | 唯一 ID |
| `reviews[].review.content` | 内容文本 |
| `reviews[].review.abstract` | 想法对应的划线原文。仅划线想法等能定位到原文的内容有值，整本书评或章节点评可能为空或不存在 |
| `reviews[].review.range` | 划线原文位置范围，格式如 `"2959-3007"`。仅能定位到原文的内容有值 |
| `reviews[].review.chapterUid` | 章节 UID。章节相关内容可能有值 |
| `reviews[].review.chapterIdx` | 章节序号。章节相关内容可能有值 |
| `reviews[].review.createTime` | 创建时间 |
| `reviews[].review.star` | 评分（0-5，-1=无评分） |
| `reviews[].review.chapterName` | 所在章节名（章节点评时有值，书评为空） |
| `reviews[].review.isFinish` | 是否读完（书评时有值） |
| `totalCount` | 总条数 |
| `hasMore` | 是否有更多（1=有） |
| `synckey` | 翻页游标（下次请求传入） |

> `abstract` 和 `range` 是条件字段：如果想法是针对某段划线/原文发表的，应优先用它们展示"原文 + 想法"；如果是整本书评、章节点评或普通想法，可能没有对应原文。

### `/book/underlines` — 章节划线热度统计

> 获取某章节每条划线的热度统计（人数/得分/类型），**不含划线文本**，主要用于阅读器内显示"X人划线"热度标签。

**请求参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `bookId` | string | 是 | 书籍 ID |
| `chapterUid` | int | 是 | 章节 UID（从 `/book/chapterinfo` 获取） |
| `synckey` | int | 否 | 增量同步 key，默认 0 |

**回包：**

| 字段 | 说明 |
|------|------|
| `bookId` | 书籍 ID |
| `chapterUid` | 章节 UID |
| `underlines` | 划线热度统计数组 |
| `underlines[].range` | 划线位置范围（如 "393-401"） |
| `underlines[].count` | 划线人数 |
| `underlines[].score` | 热度分数 |
| `underlines[].type` | 划线类型 |
| `synckey` | 同步 key |

### `/book/bestbookmarks` — 书籍热门划线

> 获取全书的 Popular Highlights，**包含划线原文和划线人数**，按热度排序。服务端固定返回前 20 条（`count=20, maxIdx=0`），不支持分页。

**请求参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `bookId` | string | 是 | 书籍 ID |
| `chapterUid` | int | 否 | 章节 UID（0=全部章节，从 `/book/chapterinfo` 获取），默认 0 |
| `synckey` | int | 否 | 增量同步 key，默认 0 |

**回包：**

| 字段 | 说明 |
|------|------|
| `synckey` | 同步 key（数据版本号） |
| `totalCount` | 热门划线总数 |
| `items` | 热门划线数组 |
| `items[].bookId` | 书籍 ID |
| `items[].userVid` | 代表用户 VID |
| `items[].bookmarkId` | 划线唯一 ID |
| `items[].chapterUid` | 所在章节 UID |
| `items[].range` | 划线位置范围（如 "393-401"） |
| `items[].markText` | 划线原文文本 |
| `items[].totalCount` | 划线人数 |
| `items[].simplifiedRange` | 简体书籍的 range（繁简体书专属） |
| `items[].traditionalRange` | 繁体书籍的 range（繁简体书专属） |
| `chapters` | 章节信息数组（用于定位划线所属章节） |
| `chapters[].bookId` | 书籍 ID |
| `chapters[].chapterUid` | 章节 UID |
| `chapters[].chapterIdx` | 章节序号 |
| `chapters[].title` | 章节标题 |

### `/book/readreviews` — 划线下的想法/评论

**请求参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `bookId` | string | 是 | 书籍 ID |
| `chapterUid` | int | 是 | 章节 UID |
| `reviews` | array | 是 | 要查询的划线范围数组 |
| `reviews[].range` | string | 是 | 划线位置范围（从 `/book/bestbookmarks` 获取） |
| `reviews[].maxIdx` | int | 否 | 翻页偏移，默认 0 |
| `reviews[].count` | int | 否 | 每页数量，服务端上限 20，超过自动截断 |
| `reviews[].synckey` | int | 否 | 翻页游标，默认 0 |

**回包：**

| 字段 | 说明 |
|------|------|
| `bookId` | 书籍 ID |
| `chapterUid` | 章节 UID |
| `reviews` | 每个 range 的想法列表 |
| `reviews[].range` | 划线范围 |
| `reviews[].totalCount` | 该范围下想法总数 |
| `reviews[].hasMore` | 是否有更多（1=有） |
| `reviews[].maxIdx` | 翻页偏移 |
| `reviews[].synckey` | 翻页游标 |
| `reviews[].pageReviews` | 想法数组 |
| `reviews[].pageReviews[].reviewId` | 想法 ID |
| `reviews[].pageReviews[].review` | 想法详情对象 |
| `reviews[].pageReviews[].review.abstract` | 划线原文（想法对应的划线内容） |
| `reviews[].pageReviews[].review.content` | 想法内容 |
| `reviews[].pageReviews[].review.range` | 划线位置范围 |
| `reviews[].pageReviews[].review.createTime` | 创建时间 |
| `reviews[].pageReviews[].review.author` | 作者信息 |

### `/review/single` — 单条想法详情

**请求参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `reviewId` | string | 是 | 想法/评论 ID |
| `commentsCount` | int | 否 | 拉取评论数量，默认 10 |
| `commentsDirection` | int | 否 | 评论排序方向：0=倒序, 1=正序 |
| `likesCount` | int | 否 | 拉取点赞数量，默认 10 |
| `likesDirection` | int | 否 | 点赞排序方向：0=倒序 |
| `synckey` | int | 否 | 增量同步 key，默认 0 |

**回包：**

| 字段 | 说明 |
|------|------|
| `reviewId` | 想法 ID |
| `review` | 想法详情对象（content, bookId, chapterUid, createTime, author 等） |
| `htmlContent` | 富文本内容 |
| `synckey` | 同步 key |

## 工作流

1. **无参数/问笔记数量排行**：调 `/user/notebooks` 展示笔记本概览；如需完整排行，必须按 `count` + `lastSort` 遍历到 `hasMore=0`，且所有分页参数平铺在 body 顶层；每本书笔记数按 `reviewCount + noteCount + bookmarkCount` 计算并排序。
2. **有 bookId 或书名，问单本书笔记内容**：同时调 `/book/bookmarklist`（划线内容）和 `/review/list/mine`（想法/点评内容），合并展示当前可导出的笔记内容。
3. **明确要求书签内容**：说明当前接口只在 `/user/notebooks` 提供书签数量，不能导出书签内容；不要把划线误当书签。
4. 用户从概览中选择某本书后，同样调上述两个接口。
5. 通过 `chapters` 中的 `chapterUid`/`title` 将划线按章节分组。
6. 翻页（notebooks）：只使用顶层平铺的 `count` + `lastSort` 游标分页；`hasMore` 为 1 时，用最后一条的 `sort` 值作为下一页 `lastSort`；禁止使用 `params` 嵌套或 `offset`/`limit`。
7. **查看书籍热门划线及想法**：
   - 调 `/book/bestbookmarks` 获取热门划线列表（含划线原文和人数）
   - 调 `/book/underlines` 获取章节内划线热度统计（人数/得分，无文本，用于展示"X人划线"标签）
   - 用 `/book/bestbookmarks` 返回的 `range` 值调 `/book/readreviews` 获取每条划线下的想法
   - 如需查看单条想法完整详情（含评论/点赞），调 `/review/single`

## 输出格式
- 笔记本概览：编号列表，每本书显示书名、作者、总笔记数、想法/点评数、划线数、书签数、阅读进度
- 单本笔记内容：按章节分组展示当前可导出的内容
  - 划线：用引用格式 `>` 标注原文
  - 想法/点评：区分划线想法、章节点评、整本书评；能关联划线时放在对应划线下方，不能关联时单独列出
  - 书签：只展示数量（来自 `/user/notebooks` 的 `bookmarkCount`），不展示内容

## 概念理清

- **统计笔记数 = `reviewCount + noteCount + bookmarkCount`**；不要把 `noteCount` 单独当作总笔记数。
- **内容导出 = 划线内容 + 想法/点评内容**；当前不能导出书签内容。
- `reviewCount` 已包含个人点评/书评想法，计算总笔记数时不要再额外加“点评数”。
- 当用户说“所有笔记内容”时，必须同时查询 `/book/bookmarklist` 和 `/review/list/mine`，不能只返回划线。
