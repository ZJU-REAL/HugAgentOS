# search — 搜索

支持多种搜索类型，通过 `scope` 参数切换 tab，来指定不同的搜索结果 tab 页面。

## 核心约束

1. 所有回复必须基于接口返回数据，不编造、不推断、不补充接口未返回的信息。
2. `scope` 选择必须基于用户意图，不要默认全部使用 `scope=10`。
3. 始终显式传 `scope`，不要依赖服务端默认值。
4. 禁止输出中间推理过程，不要在回复中暴露 scope 选择逻辑、参数拼装思路等内部过程。
5. 搜索结果是分页片段，不代表全量结果；回复中使用"为您找到"，不要使用"共有""一共""总共"等暗示结果完整的措辞。
6. 请求参数 `scope` 和回包字段 `results[].scope` 不是同一个语义：找书时仍传 `scope=10`，但电子书分组回包可能是 `results[].scope=17`；不要用 `results[].scope == 10` 过滤电子书结果。

## 接口

`/store/search`

**请求参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `keyword` | string | 是 | 搜索关键词 |
| `scope` | int | 否 | 搜索类型。接口层可省略，但 Agent 必须按下方“scope 选择指引”显式选择；未传时服务端默认 10（电子书） |
| `maxIdx` | int | 否 | 翻页偏移，默认 0 |
| `count` | int | 否 | 每页数量，不传则服务端默认 15。用户未指定数量时不要传此参数 |

**scope 对应关系：**

| scope | 名称 | 说明 |
|-------|------|------|
| `0` | 全部 | 综合搜索，results 中包含多个分组；适合用户只说“搜一下”且未限定类型 |
| `10` | 电子书 | 只搜电子书（不含网文小说）；适合用户明确“搜书/找书/搜某本书” |
| `16` | 网文小说 | 只搜网文小说 |
| `14` | 微信听书 | 有声书/专辑/播客（三者同义） |
| `6` | 作者 | 搜索作者 |
| `12` | 全文 | 搜索书籍正文内容 |
| `13` | 书单 | 搜索书单 |
| `2` | 公众号 | 搜索公众号 |
| `4` | 文章 | 搜索公众号文章 |

> `scope=17` 是服务端回包中电子书分组常见的加载/分页标识，不是 Agent 的首选请求参数。用户找书时仍按 `scope=10` 请求。

**scope 选择指引（Agent 根据用户意图自动选择）：**
- 用户明确说"搜书""找书""查某本书"或请求获取 bookId → `scope=10`（电子书）
- 用户只说"搜一下 xx"，未说明要搜书/作者/文章/公众号等具体类型 → `scope=0`（全部）
- 用户说"网文""网络小说" → `scope=16`（网文小说）；如果只是普通语义中的"小说"且想找书，仍用 `scope=10`
- 用户说"听书""有声书""播客""专辑" → `scope=14`
- 用户说"搜一下 xx 作者""查作者 xx" → `scope=6`
- 用户说"书里提到了 xx""全文搜索" → `scope=12`
- 用户说"有什么书单""推荐书单" → `scope=13`
- 用户说"搜公众号" → `scope=2`
- 用户说"搜文章" → `scope=4`
- 不要把"没特别指定"同时解释成 `scope=10` 和 `scope=0`；判断标准是：有明确找书意图用 `scope=10`，泛搜索用 `scope=0`。

## 参数提取规则

从用户输入中提取 `keyword` 时：

1. 去掉"帮我""搜一下""找一下""有没有""请问"等口语化前缀/后缀，只保留核心检索词。
2. 保留真正要搜索的实体或主题：
   - "帮我搜一下三体" → `keyword=三体`
   - "有没有刘慈欣的书" → `keyword=刘慈欣`
   - "找一本关于量子力学的入门书" → `keyword=量子力学入门`
3. 用户使用"这个作者""他的其他书"等代词时，结合对话上下文补全为实际实体名称再搜索。
4. 同时提到书名和作者时，优先使用更具区分度的词作为 `keyword`，如"刘慈欣的三体"优先搜 `三体`。

**回包（V3 格式）：**

| 字段 | 说明 |
|------|------|
| `sid` | 搜索会话 ID |
| `hasMore` | 是否有更多（1=有, 0=无） |
| `results` | 搜索结果分组数组 |
| `results[].title` | 分组标题（如"电子书""作者"） |
| `results[].scope` | 回包分组类型，不保证等于请求参数 `scope`。例如请求 `scope=10` 搜电子书时，电子书分组可能返回 `scope=17` |
| `results[].scopeCount` | 该分组总结果数 |
| `results[].currentCount` | 本次返回数量 |
| `results[].books` | 书籍/结果数组 |
| `results[].books[].searchIdx` | 搜索序号（用于翻页） |
| `results[].books[].bookInfo` | 书籍信息对象 |
| `results[].books[].bookInfo.bookId` | 书籍唯一标识 |
| `results[].books[].bookInfo.readerLink` | 阅读器跳转链接；展示为 `[打开阅读]({readerLink})` |
| `results[].books[].bookInfo.deepLink` | 书籍详情跳转链接；没有 `readerLink` 时可展示为 `[查看详情]({deepLink})` |
| `results[].books[].bookInfo.title` | 书名 |
| `results[].books[].bookInfo.author` | 作者 |
| `results[].books[].bookInfo.cover` | 封面图 URL |
| `results[].books[].bookInfo.intro` | 书籍简介 |
| `results[].books[].bookInfo.publisher` | 出版社 |
| `results[].books[].bookInfo.category` | 分类 |
| `results[].books[].bookInfo.payType` | 付费类型 |
| `results[].books[].bookInfo.price` | 价格（分） |
| `results[].books[].bookInfo.soldout` | 是否下架 |
| `results[].books[].readingCount` | 在读人数 |
| `results[].books[].newRating` | 评分（0-100） |
| `results[].books[].newRatingCount` | 评分人数 |
| `results[].books[].newRatingDetail` | 评分标签（如 `{"title":"神作"}` ） |

> `scope=0`（全部）时 results 会返回多个分组（电子书、作者、书单等），每个分组有自己的 title 和 scope。展示时按回包内容处理，不要把请求 scope 当作分组过滤条件。

## 工作流

1. 根据用户意图选择 `scope`，调 `/store/search`，并显式传入 `scope`。
2. 从 `results` 取搜索结果。单 tab 模式（scope>0）可能返回多个同类分组；全部模式（scope=0）有多个分组。
3. 展示结果：书名、作者、评分、在读人数、分类。已下架（soldout=1）需标注；如有 `readerLink`，展示为 `[打开阅读]({readerLink})`；否则如有 `deepLink`，展示为 `[查看详情]({deepLink})`。
4. 用户选择某本书后，调 `/book/info` 获取完整信息。
5. 翻页：`hasMore` 为 1 时，用最后一条的 `searchIdx` 作为下一页的 `maxIdx`。

### 结果处理细则

- 用户找书时请求 `scope=10`；回包中标题为"电子书"或包含 `books` 的分组都可以展示，不要要求 `results[].scope == 10`。

## 输出格式
- 搜索结果用编号列表展示，方便用户通过数字选择
- scope=0 时按分组标题（电子书/作者/书单…）分区展示
- 重点展示：书名、作者、评分、在读人数、分类
- 空结果（`results` 为空数组或无匹配项）时回复：`抱歉，没有找到与"{keyword}"相关的结果。你可以换个关键词、检查错别字，或尝试更简短的关键词。`
- 接口调用失败时回复：`搜索服务暂时不可用，请稍后再试。如果持续出现问题，可以换个关键词尝试。`
