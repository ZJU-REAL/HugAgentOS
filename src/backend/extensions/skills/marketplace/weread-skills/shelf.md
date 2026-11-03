# shelf — 书架管理

## 重要概念

**专辑 = 有声书**，两者是同一概念。微信读书中，有声书/听书内容以"专辑"形式存在，存放在书架的 `albums` 字段中，与 `books`（电子书）完全独立。

**书架里的“书”包含电子书和专辑/有声书。** 当用户问“我的书架里有多少本书”“书架有多少本”“书架总数”时，不能只数 `books[]`，必须同时计入 `albums[]`。

常见错误：
- ⚠️ **不要**通过遍历 `books` 逐个调 `/book/info` 检查 `format` 来判断有声书——`/book/info` 不返回 format 字段，且效率极低。直接使用 `albums` 字段即可。
- ⚠️ 书架数量必须用实际返回数组计算，且必须包含 `albums[]`；不要只用 `books.length` 回答“书架里有多少本书”。
- ⚠️ 公开/私密阅读数量也必须遍历实际返回条目，不能使用任何未出现在数组中的补丁项。

## 接口

`/shelf/sync`

**请求参数：** 无（用户身份通过 API Key 自动识别）

**回包：**

| 字段 | 说明 |
|------|------|
| `books[]` | 可枚举的电子书/导入书/公众号类书籍条目数组，不含 `albums[]`，也不含 `mp` 文章收藏入口 |
| `books[].bookId` | 书籍唯一标识 |
| `books[].readerLink` | 阅读器跳转链接；展示为 `[打开阅读]({readerLink})` |
| `books[].deepLink` | 书籍详情跳转链接；没有 `readerLink` 时可展示为 `[查看详情]({deepLink})` |
| `books[].title` | 书名 |
| `books[].author` | 作者 |
| `books[].cover` | 封面图 URL |
| `books[].category` | 分类 |
| `books[].readUpdateTime` | 最近阅读时间（Unix 时间戳） |
| `books[].finishReading` | 是否读完（1=读完） |
| `books[].updateTime` | 书籍更新时间 |
| `books[].isTop` | 是否置顶 |
| `books[].secret` | 是否私密（1=私密） |
| `albums[]` | 专辑/有声书数组（与 books 完全独立） |
| `albums[].albumInfo.albumId` | 专辑唯一标识 |
| `albums[].albumInfo.name` | 专辑名称 |
| `albums[].albumInfo.authorName` | 演播/作者 |
| `albums[].albumInfo.cover` | 封面图 URL |
| `albums[].albumInfo.trackCount` | 音频集数 |
| `albums[].albumInfo.finishStatus` | 完结状态（如"已完结"） |
| `albums[].albumInfo.finish` | 是否完结（1=完结） |
| `albums[].albumInfo.payType` | 付费类型 |
| `albums[].albumInfo.intro` | 专辑简介 |
| `albums[].albumInfo.updateTime` | 更新时间（Unix 时间戳） |
| `albums[].albumInfoExtra.secret` | 是否私密 |
| `albums[].albumInfoExtra.lecturePaid` | 是否已购买（1=已购买） |
| `albums[].albumInfoExtra.lectureReadUpdateTime` | 最近收听时间 |
| `albums[].albumInfoExtra.isTop` | 是否置顶 |
| `mp` | 文章收藏入口对象；只表示“文章收藏”目录入口，不包含具体文章内容；非空时表示书架界面有 1 个“文章收藏”条目，不包含在 `books[]`/`albums[]` 中 |
| `archive[].name` | 书单名称 |
| `archive[].bookIds` | 书单内的 bookId 列表 |
| `bookCount` | 可枚举电子书数量，通常等于 `books[].length`；不含 `albums[]` 和 `mp` |

## 数量口径

| 用户问题/指标 | 正确计算方式 | 说明 |
|------|------|------|
| 书架界面有多少本/多少条目 | `books.length + albums.length + (mp 非空 ? 1 : 0)` | 默认回答这个口径；用户说“书架里的书”时也包含专辑/有声书 |
| 电子书数 | `bookCount` 或 `books.length` | 仅 `books[]`，不含专辑和文章收藏；只有用户明确问“电子书”时才用这个口径 |
| 有声书/专辑数 | `albums.length` | 专辑按有声书管理，也是书架总数的一部分 |
| 是否有文章收藏 | `mp 非空 ? 1 : 0` | `mp` 是单独入口，但其中不包含文章收藏的具体内容 |

**⚠️ 强制回答规则**：当用户问任何书架数量问题时，必须使用实际可枚举数组计算：`books.length + albums.length + (mp 非空 ? 1 : 0)`。其中 `albums.length` 必须计入，因为专辑/有声书在书架里也按“书”管理。不要使用其他服务端内部计数字段或基于内部计数字段的公式。

### Few-shot：正确计算书架总数

**例 1：有电子书和专辑，无文章收藏**

回包关键信息：
- `books.length = 10`
- `albums.length = 3`
- `mp` 为空

用户问：“我的书架里有多少本书？”

正确回答：
> 你的书架共有 **13 个条目**：10 本电子书 + 3 个专辑/有声书。

错误回答：
> 你的书架共有 10 本书，另外还有 3 个有声书。

错误原因：用户问的是书架里的书，专辑/有声书也在书架里按“书”管理，必须计入总数，不能“另外还有”。

**例 2：无专辑，有文章收藏**

回包关键信息：
- `books.length = 15`
- `albums.length = 0`
- `mp` 非空
- 统计出 `books` 中包含 13 个公开阅读书籍 + 2 个私密阅读书籍


用户问：“我的书架有多少本书？”

正确回答：
> 你的书架共有 **16 个条目**：15 个书籍条目 + 1 个文章收藏；其中公开阅读 13 个、私密阅读 3 个。
解释：`mp` 非空时，文章收藏计入书架总数，并固定计入私密阅读数量。

错误回答：
> 你的书架共有 15 本纯书籍，另外还有 1 个文章收藏。
错误原因：用户问的是书架总数，文章收藏必须计入总数，不能用“另外还有”把它排除在总数之外。

**例 3：有专辑，有文章收藏**

回包关键信息：
- `books.length = 130`
- `albums.length = 3`
- `mp` 非空

用户问：“我的书架一共有多少本？”

正确回答：
> 你的书架可见条目共有 **134 个**：130 个书籍条目 + 3 个专辑/有声书 + 1 个文章收藏。

错误回答：
> 你的书架共有 133 个条目，另外还有 1 个文章收藏。

错误原因：文章收藏必须计入书架总数，不能用“另外还有”把它排除在总数之外。

## 公开/私密阅读数量

公开/私密阅读必须遍历实际返回条目：

- **私密阅读数** = `books[].secret == 1` 的数量 + `albums[].albumInfoExtra.secret == 1` 的数量 + (`mp` 非空 ? 1 : 0)
- **公开阅读数** = `books[].secret == 0` 的数量 + `albums[].albumInfoExtra.secret == 0` 的数量
- `mp` 只表示文章收藏目录入口，不包含具体内容；如果 `mp` 不存在则不影响公开/私密数量，如果 `mp` 非空则私密阅读数量固定 +1。

注意：只统计 `books[]`、`albums[]` 和 `mp` 这些实际返回的可见条目；未出现在数组中的服务端补丁项不能纳入公开/私密分组。

## 工作流

1. 调 `/shelf/sync` 获取书架列表。
2. 如果用户问“书架有多少本书/多少条目”，先计算可见条目数：`total = books.length + albums.length + (mp 非空 ? 1 : 0)`；注意 `albums[]` 是专辑/有声书，也必须计入书架里的书。
3. 如果用户问公开/私密阅读数量，遍历 `books[]` 和 `albums[]` 的 `secret` 字段分组计数；`mp` 不看 `secret`，只要非空就给私密阅读数量 +1。
4. 展示：书名、作者、分类，置顶书籍（`isTop`）标记提示，显示总数。
5. 查询有声书/专辑数量：直接读取 `albums` 数组长度即可，无需额外接口调用。
6. 用户选择某本书后，调 `/book/info` 获取详情。
7. 调 `/book/getprogress` 可查看某本书的阅读进度。

## 输出格式
- 书架列表用编号展示，支持通过编号选择查看详情
- 无参数时显示书架全览，第一句给出可见书架条目数：`books.length + albums.length + (mp 非空 ? 1 : 0)`；其中 `albums[]` 必须作为专辑/有声书计入书架总数
- 如果展示分类构成，各分类数量相加必须等于可见书架条目数；`mp` 非空时，文章收藏作为 1 个书架条目计入总数
- 公开/私密阅读数量必须展示为遍历 `books[]`、`albums[]` 后得到的分组计数，并在 `mp` 非空时给私密阅读数量 +1
- 传书名/bookId 时查看该书详情或进度
- 涉及有声书/专辑/听书的问题，直接使用 `albums` 字段回答
- 接口调用失败时回复：`书架信息暂时没拉到，请稍后再试~`
