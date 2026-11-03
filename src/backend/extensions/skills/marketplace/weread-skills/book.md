# book — 书籍信息与阅读进度

查看书籍详情、章节目录、阅读进度。

## 接口

### `/book/info` — 书籍基本信息

**请求参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `bookId` | string | 是 | 书籍 ID |

**回包：**

| 字段 | 说明 |
|------|------|
| `bookId` | 书籍 ID |
| `readerLink` | 阅读器跳转链接；展示为 `[打开阅读]({readerLink})` |
| `deepLink` | 书籍详情跳转链接；没有 `readerLink` 时可展示为 `[查看详情]({deepLink})` |
| `title` | 书名 |
| `author` | 作者 |
| `translator` | 译者 |
| `cover` | 封面 URL |
| `intro` | 简介 |
| `category` | 分类 |
| `publisher` | 出版社 |
| `publishTime` | 出版时间 |
| `isbn` | ISBN |
| `wordCount` | 总字数 |
| `newRating` | 评分（百分制） |
| `newRatingCount` | 评分人数 |
| `newRatingDetail` | 评分分布详情 |

### `/book/chapterinfo` — 章节目录

**请求参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `bookId` | string | 是 | 书籍 ID |

**回包：**

| 字段 | 说明 |
|------|------|
| `bookId` | 书籍 ID |
| `synckey` | 同步 key（版本号） |
| `chapterUpdateTime` | 章节最后更新时间 |
| `chapters` | 章节数组 |
| `chapters[].chapterUid` | 章节 UID（用于其他接口如 underlines） |
| `chapters[].chapterIdx` | 章节序号 |
| `chapters[].title` | 章节标题 |
| `chapters[].wordCount` | 章节字数 |
| `chapters[].level` | 目录层级（1=一级标题, 2=二级…） |
| `chapters[].updateTime` | 章节更新时间 |
| `chapters[].price` | 章节价格（0=免费） |
| `chapters[].paid` | 是否已购买（1=已购买） |
| `chapters[].isMPChapter` | 是否公众号章节（1=是） |
| `chapters[].anchors` | 章节内锚点/子标题数组 |

### `/book/getprogress` — 阅读进度

**请求参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `bookId` | string | 是 | 书籍 ID |

**回包：**

| 字段 | 说明 |
|------|------|
| `bookId` | 书籍 ID |
| `book.chapterUid` | 当前阅读章节 UID |
| `book.chapterOffset` | 当前章节内偏移 |
| `book.progress` | 阅读进度百分比（整数，0-100）。**注意：1 表示 1%，不是 100%**。0=未读，1-99=部分阅读（如 1=仅翻了几页），100=已读完。只有 100 才代表读完 |
| `book.updateTime` | 最后阅读时间 |
| `book.recordReadingTime` | 累计阅读时长（秒） |
| `book.finishTime` | 读完时间（仅 progress=100 时存在，否则无此字段） |
| `book.isStartReading` | 是否已开始阅读 |
| `timestamp` | 服务端时间戳 |

## 工作流

1. **查看书籍详情**：用户提供 bookId 或书名（书名先调 `/store/search`），调 `/book/info` 获取基本信息。
2. **查看章节目录**：调 `/book/chapterinfo`，按 level 层级缩进展示目录结构。
3. **查看阅读进度**：调 `/book/getprogress`，展示阅读百分比和累计时长。
4. `chapterUid` 是后续查看章节划线热度（`/book/underlines`）和热门划线（`/book/bestbookmarks`）等接口的参数。

## 输出格式
- 书籍详情：展示书名、作者、评分、简介等核心信息
- 章节目录：按层级缩进展示，标注字数和付费状态
- 阅读进度：展示百分比和阅读时长（转为小时/分钟）。**progress 是 0-100 的整数，必须带 % 号展示**（如 progress=1 展示为"1%"，progress=45 展示为"45%"）。只有 progress=100 且有 finishTime 时才表示已读完
- 书籍信息获取失败时回复：`暂时无法获取书籍信息，请稍后再试~`
- 无结果时根据用户问题自然说明没有找到对应书籍或记录，不要编造数据
