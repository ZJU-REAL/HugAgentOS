# profile — 用户信息与阅读统计

## 说明

通过组合 `/shelf/sync`、`/book/getprogress`、`/user/notebooks` 获取用户阅读概况。

## 核心约束

1. 所有回复必须基于接口返回数据，不编造、不推断。
2. 禁止输出中间推理过程，不暴露接口选择、参数拼装等内部过程。
3. 书架数量必须按 `books.length + albums.length + (mp 非空 ? 1 : 0)` 计算；`albums[]` 是专辑/有声书，也属于书架里的书，不能只统计 `books[]`。
4. 阅读进度只查最近 5 本电子书：从 `books[]` 按 `readUpdateTime` 降序取前 5 本，逐个调 `/book/getprogress`，避免大书架导致过多调用。
5. 阅读时长字段单位为秒，展示时转为"X小时Y分钟"格式；Unix 时间戳展示为 `YYYY-MM-DD`。
6. 展示书籍时，优先将回包中的 `readerLink` 展示为 `[打开阅读]({readerLink})`；没有 `readerLink` 但有 `deepLink` 时，展示为 `[查看详情]({deepLink})`；不要手动拼接链接。

## 工作流

### 1. 获取书架
调 `/shelf/sync`，了解用户在读什么书、总数等。
书架数量必须按 `books.length + albums.length + (mp 非空 ? 1 : 0)` 计算；`albums[]` 是专辑/有声书，也属于书架里的书，不能只统计 `books[]`。
具体逻辑参考 `shelf.md`。

### 2. 获取阅读进度
从 `books[]` 中按 `readUpdateTime` 降序取最近阅读的前 5 本书，分别调 `/book/getprogress` 获取进度和累计阅读时长，具体见 `book.md`。

注意：
- 只对 `books[]` 中的电子书调 `/book/getprogress`，不要对 `albums[]` 专辑/有声书调用该接口。
- `book.progress` 是 0-100 的整数，展示时必须带 `%`；只有 `progress=100` 且存在 `finishTime` 时才表示已读完。
- `book.recordReadingTime` 是累计阅读时长，单位为秒。

### 3. 获取笔记
调 `/user/notebooks` 获取笔记概览，展示有笔记的书籍数和总笔记数。这里只做概览，不导出单本书笔记内容。

如果用户要查看某本书的具体笔记内容，再按 `notes.md` 调 `/book/bookmarklist` 和 `/review/list/mine`。

### 4. 综合展示
汇总书架总数、最近阅读进度和笔记概况。

## 输出格式
- 无参数时展示阅读概况：书架总数 + 最近阅读的前 5 本进度 + 笔记概况。
- 书架总数第一句给出可见条目数，并拆分电子书、专辑/有声书、文章收藏入口数量。
- 每本书显示：书名、作者、进度百分比、累计阅读时长、最近阅读时间；优先附 `[打开阅读]({readerLink})`，没有 `readerLink` 时可附 `[查看详情]({deepLink})`。
- 笔记概况显示：有笔记的书籍数、总笔记数。
- 书架为空时回复：`你的书架还没有书哦，要不要去发现页看看推荐？`
- 接口调用失败时回复：`阅读概况暂时无法获取，请稍后再试~`
