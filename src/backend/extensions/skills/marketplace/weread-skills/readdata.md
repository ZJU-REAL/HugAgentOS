# readdata — 阅读统计

查看个人阅读数据统计，包含阅读时长、天数、读书排行、偏好分析等。

> **⚠️ 使用前必须阅读本文件字段说明。** 阅读统计字段容易因字段名产生误判，尤其是所有阅读时长字段的单位。调用 `/readdata/detail` 前必须先确认本文件中的参数、字段单位和统计口径；禁止凭字段名或数值大小推断单位。

## 接口

### `/readdata/detail` — 阅读统计详情

**请求参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `mode` | string | 否 | 统计维度：`weekly`=本周, `monthly`=本月, `annually`=本年, `overall`=总计。默认 `monthly`。|
| `baseTime` | int | 否 | 基准时间戳（0=当前周期），此时服务端会归一化到周期起点：周一、月初、年初；`overall` 固定为 0。传历史时间戳可查看该时间戳所在周期的数据；`annually` 只返回 `baseTime` 所在自然年的数据，不会自动包含后续年份 |

**回包字段说明（字段按 `mode` 和数据条件可选返回）：**

| 字段 | 说明 |
|------|------|
| `baseTime` | 统计周期的基准时间戳：`weekly` 为周一 00:00，`monthly` 为月初 00:00，`annually` 为年初 00:00，`overall` 为 0 |
| `readTimes` | 分桶阅读/收听总时长（对象，key 为分桶起始时间戳，value 为秒数）。`weekly`/`monthly` 通常按天分桶，`annually` 按月分桶，`overall` 按年分桶 |
| `dailyReadTimes` | 年度模式可能返回的每日阅读时长明细（对象，key 为日期时间戳，value 为秒数）；用于日历明细展示，不应替代 `totalReadTime` 作为总量口径 |
| `readDays` | 有效阅读天数。服务端按有效阅读规则计算，当前规则为单日阅读满 1 分钟 |
| `totalReadTime` | 当前请求周期的总阅读/收听时长（**秒**）。统计总时长时优先使用该字段，`readTimes` 仅用于明细展示或交叉校验；**禁止误当成分钟或小时** |
| `dayAverageReadTime` | 日均阅读/收听时长（秒），分母是当前周期已过去的自然日数或历史完整周期自然日数，不是 `readDays` |
| `compare` | 与上一周期的日均时长对比比例；正数表示增长，负数表示下降。该字段只在当前周期且上一周期数据足够时返回，`0.2` 表示约增长 20% |
| `readLongest` | 读得最多的书/有声内容排行数组，最多 10 条，按 `readTime` 降序；低于 5 分钟的条目会被过滤 |
| `readLongest[].book` | 书籍信息对象（电子书/出版书），包含 `bookId`、`title`、`author`、`cover` 等 |
| `readLongest[].albumInfo` | 有声内容信息对象；当排行条目是有声书/专辑时返回 |
| `readLongest[].readTime` | 该书或有声内容在当前统计范围内的阅读/收听时长（秒） |
| `readLongest[].recordReadingTime` | 该书的朗读/记录类阅读时长（秒），存在时才返回 |
| `readLongest[].tags` | 标签数组，目前常见值包括 `笔记最多`、`单日阅读最久` |
| `readStat` | 阅读统计摘要数组 |
| `readStat[].stat` | 统计项名称，常见为 `读过`、`读完`、`阅读`、`笔记` |
| `readStat[].counts` | 统计值文案，如 `12本`、`45天`、`120条` |
| `readStat[].scheme` | 对应统计项的 App 跳转链接，可能为空 |
| `preferCategory` | 偏好阅读分类数组，最多 8 个；不足时可能补充默认分类占位 |
| `preferCategory[].categoryId` | 分类 ID |
| `preferCategory[].categoryTitle` | 分类名称 |
| `preferCategory[].parentCategoryId` | 父分类 ID |
| `preferCategory[].parentCategoryTitle` | 父分类名称 |
| `preferCategory[].val` | 分类偏好权重，按最高分类阅读时长归一化后的相对值，用于图表展示 |
| `preferCategory[].readingTime` | 该分类阅读时长（秒） |
| `preferCategory[].readingCount` | 该分类阅读本数 |
| `preferCategory[].categoryType` | 分类类型标记，普通分类为 0，部分特殊分类会返回 1 或 2 |
| `preferCategoryWord` | 偏好分类文案，如 `偏好阅读文学`；年度报告场景可能改为固定文案 `偏好阅读` |
| `preferTime` | 24 小时阅读时段分布数组，值为秒数。注意输出顺序从 6 点开始，依次到次日 5 点，不是从 0 点开始 |
| `preferTimeWord` | 偏好时段文案。总偏好时段数据不足 10 小时时可能不返回；常见文案如 `偏好上午阅读`、`偏好白天阅读`、`偏好夜间阅读`、`汲取新知，昼夜不倦` |
| `preferAuthor` | 偏好作者数组。只有作者数据达到展示阈值时返回 |
| `preferAuthor[].authorId` | 作者 ID |
| `preferAuthor[].name` | 作者名 |
| `preferAuthor[].count` | 阅读该作者的书本数 |
| `preferAuthor[].readTime` | 阅读该作者作品的时长，格式化字符串，如 `5小时30分钟`，不是秒数 |
| `preferAuthor[].user` | 作者关联用户信息，存在时返回 |
| `authorCount` | 符合统计条件的作者总数，不一定等于 `preferAuthor` 返回条数 |
| `preferPublisher` | 偏好出版社数组。至少 3 个出版社且最高出版社阅读本数达到阈值时返回 |
| `preferPublisher[].name` | 出版社名 |
| `preferPublisher[].count` | 阅读该出版社书籍的本数 |
| `preferCp` | 偏好版权方数组。满足展示阈值时返回 |
| `preferCp[].count` | 阅读该版权方书籍的本数 |
| `preferCp[].copyrightInfo` | 版权方信息，包括名称、用户 VID、头像、角色等 |
| `readRate` | 文字阅读占比百分比，计算口径约为 `wrReadTime / (wrReadTime + wrListenTime) * 100`。当总时长不足 1 小时或文字阅读占比过高时不返回 |
| `wrReadTime` | 文字阅读时长（秒），通常为 `totalReadTime - wrListenTime`；仅在 `readRate` 可展示时返回 |
| `wrListenTime` | 听书/TTS/有声内容时长（秒）；仅在 `readRate` 可展示时返回 |
| `rank` | 本周好友阅读排行信息；仅当前周且未隐藏排行时返回 |
| `rank.text` | 排行文案，如 `朋友中排第3名` |
| `rank.scheme` | 排行跳转链接 |
| `registTime` | 用户注册时间戳 |
| `medals` | 勋章数组；可展示勋章不少于 3 个时返回 |
| `preferBooks` | 偏好阅读书籍卡片数组，包含书籍、推荐理由和偏好类型等信息 |
| `yearReport` | 年度报告入口数组。`overall` 可能返回多年的入口，`annually` 可能返回当前年份入口；`times` 为该年 12 个月阅读/收听时长数组 |
| `recordReadingTime` | 总朗读/记录类阅读时长（秒），目前主要在 `overall` 模式下汇总返回 |
| `readRecordsWord` | 书籍分布模块标题文案，当前固定为 `书籍分布` |
| `readDistributionWord` | 点评分布模块标题文案，当前固定为 `点评分布` |
| `readTimeGears` | 阅读时长档位数组，当前为 `[60, 1800, 3600, 10800, 18000]`，用于前端展示分段 |
| `styleType` | 样式类型，常见为 `normal`；年度报告场景可能返回特殊样式 |

> 年度报告相关字段（如 `annualList2023`、`preferBooks2023`、2025 年报模块字段等）会随活动配置变化，不作为通用阅读统计字段依赖。

## 周期特点与区间组合

`/readdata/detail` 只支持按固定自然周期查询，不支持直接传任意起止日期。遇到"某天至今"、"某月中旬到现在"、"跨年区间"这类请求时，应通过多个固定周期结果组合计算。

| mode | 周期粒度 | baseTime 行为 | 适合用途 |
|------|----------|---------------|----------|
| `weekly` | 自然周 | 归一到该周周一 00:00 | 本周、某历史周 |
| `monthly` | 自然月 | 归一到该月 1 日 00:00 | 本月、某历史月、区间边界扣减 |
| `annually` | 自然年 | 归一到该年 1 月 1 日 00:00 | 某年全年、今年至今、跨年区间拼接 |
| `overall` | 全部历史 | 固定为 0 | 总计，不适合拆任意日期区间 |

**组合原则：**

1. 优先用较大周期减少调用次数：整年用 `annually`，整月用 `monthly`。
2. 跨年区间按自然年拆分：历史整年 + 当前年至今。
3. 起点落在年/月中间时，可用"大周期 - 起点之前的完整小周期"近似组合；如果接口返回 `dailyReadTimes`，可对边界日期做日级精确扣减。
4. **完整周期**使用该周期回包的 `totalReadTime`；**不完整边界周期**优先使用 `dailyReadTimes` 精确扣除起点前/终点后的日期。若没有日级明细，只能使用月级/年级近似，并在回答中说明口径。
5. 不要把截断展示的 `readTimes` 当作主结果；`readTimes` 仅用于明细展示或交叉校验。

**Few-shot：**

- 用户问："2024 年 1 月 31 日至今，我的总阅读时长是多少？"
  - 推荐做法：查询 `2024` 至当前年份的 `annually`，累加年度 `totalReadTime`；再查询 `2024-01` 的 `monthly`，从总和中扣除 2024 年 1 月的 `totalReadTime`，得到近似的 `2024-02-01 至今` 口径。
  - 若年度返回 `dailyReadTimes` 且需要精确到 1 月 31 日，则只扣除 `2024-01-01` 至 `2024-01-30` 的日级时长，保留 1 月 31 日。
- 用户问："2025 年以来读了多久？"
  - 查询 `mode=annually`，`baseTime` 取 2025 年内任一时间戳；如果当前年份大于 2025，再继续查询后续每个自然年并累加。
- 用户问："去年 3 月到今年 2 月读了多久？"
  - 查询去年 3-12 月各月 `monthly`，再查询今年 1-2 月各月 `monthly`，累加 `totalReadTime`。

## 工作流

1. **默认**：调 `/readdata/detail`，不传参数使用默认 `mode=monthly` 展示本月阅读数据。
2. **用户问本周/今年/总共**：对应传 `mode=weekly`/`annually`/`overall`。
3. **用户问历史数据**：如"上个月读了多少"，将上月某天的时间戳作为 `baseTime` 传入；如"2025 年读了多久"，传 `mode=annually` 且 `baseTime` 取 2025 年内任一时间戳。
4. **用户问跨年区间**：如"2024 年至今""2025 年以来"，必须按自然年逐年查询：从起始年份到当前年份分别调用 `mode=annually`，每次 `baseTime` 取该年份内时间戳；历史年份返回的是该自然年全年数据，当前年份返回的是本年至今数据。不要把 `2025` 年度结果标注为"2025 年至今"，也不要漏查当前年份。
5. **用户问任意起止日期区间**：先判断是否能拆成完整自然年/月/周；完整周期使用 `totalReadTime` 累加，不完整边界优先使用 `dailyReadTimes` 做日级扣减；如果没有日级明细，则使用月级近似并说明口径。例如"2024 年 1 月 31 日至今"可用 2024 年至今的年度数据合计，减去 2024 年 1 月月度数据，得到 `2024-02-01 至今` 口径；若有日级明细，则只扣除 1 月 1-30 日，保留 1 月 31 日。
6. **总时长口径**：单个完整周期优先采用回包 `totalReadTime`；跨周期区间按"完整周期 `totalReadTime` 累加/相减 + 边界周期 `dailyReadTimes` 日级修正"计算。不要手动把截断输出里的 `readTimes` 相加作为主结果；回答时明确标注使用了哪些完整周期，以及是否使用了日级边界扣减或月级近似。
7. **日均口径**：`dayAverageReadTime` 是按自然日平均，不是按阅读天数平均；如果需要“阅读日均”，必须说明该字段不是接口直接返回值，需要用 `totalReadTime / readDays` 另行计算。
8. 综合展示：总时长、阅读天数、自然日均时长、与上期对比，读得最多的书，偏好分类和作者。

## 输出格式

- **总览**：阅读天数、总时长（转为 x 小时 y 分钟）、自然日均时长、与上期对比（增长/下降百分比）
- **读书排行**：列出读得最多的书/有声内容，书名或专辑名 + 阅读/收听时长
- **阅读统计**：读过本数、读完本数、阅读天数、笔记数等
- **偏好分析**：偏好分类、偏好时段、偏好作者、偏好出版社/版权方（如有）
- 时长单位统一转换：所有阅读时长字段均按秒处理，秒 → "x 小时 y 分钟"格式；不得把 `totalReadTime` 当成分钟或小时
- 接口调用失败时回复：`阅读数据暂时无法获取，请稍后再试~`
