# review — 书籍点评

书籍的公开点评（区别于个人笔记/划线，个人笔记见 `notes.md`）。

## 核心约束

1. 所有回复必须基于接口返回数据，不编造、不推断。
2. 公开点评不属于个人笔记，是其他读者对书籍的评价；个人笔记/划线见 `notes.md`。
3. 评分转换：20=一星，40=二星，60=三星，80=四星，100=五星。
4. 禁止输出中间推理过程，不暴露接口选择、参数拼装等内部过程。

## 接口

### `/review/list` — 书籍公开点评

**请求参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `bookId` | string | 是 | 书籍 ID |
| `reviewListType` | int | 否 | 筛选类型：0=全部, 1=推荐, 2=不行, 3=最新, 4=一般。默认 0 |
| `count` | int | 否 | 每页数量，默认 20 |
| `maxIdx` | int | 否 | 翻页偏移，默认 0 |
| `synckey` | int | 否 | 翻页游标，默认 0 |

**回包（经裁剪）：**

| 字段 | 说明 |
|------|------|
| `synckey` | 翻页游标（下次请求传入） |
| `reviewsCnt` | 点评总数 |
| `recentTotalCnt` | 最新点评数 |
| `reviewsHasMore` | 是否有更多点评（1=有） |
| `reviewsHas5Star` | 是否有五星推荐点评（1=有） |
| `reviewsHas1Star` | 是否有一星差评（1=有） |
| `reviewsHasRecent` | 是否有最新点评（1=有） |
| `friendCommentCount` | 好友点评数 |
| `friendUniqueCount` | 点评好友数 |
| `friendCommentUsers` | 点评好友信息数组 |
| `friendCommentUsers[].userVid` | 好友 vid |
| `friendCommentUsers[].name` | 好友昵称 |
| `friendCommentUsers[].avatar` | 好友头像 |
| `deepVRecommendInfo` | 资深会员推荐摘要 |
| `deepVRecommendInfo.title` | 如"2337 个资深会员点评" |
| `deepVRecommendInfo.subtitle` | 如"其中 2015 人(86.2%)推荐本书" |
| `deepVRecommendValue` | 资深会员推荐比例（862 = 86.2%） |
| `deepVUniqueCount` | 点评资深会员数 |
| `reviews` | 点评数组 |
| `reviews[].idx` | 序号（用于翻页，下次 maxIdx 传最后一条的 idx） |
| `reviews[].review.reviewId` | 点评唯一 ID |
| `reviews[].review.review.content` | 点评文本内容 |
| `reviews[].review.review.htmlContent` | 点评 HTML 内容（富文本） |
| `reviews[].review.review.star` | 评分（20=一星, 40=二星, 60=三星, 80=四星, 100=五星） |
| `reviews[].review.review.isFinish` | 是否读完此书 |
| `reviews[].review.review.createTime` | 创建时间 |
| `reviews[].review.review.chapterName` | 所在章节名（章节点评时有值） |
| `reviews[].review.review.author.userVid` | 评论者 vid |
| `reviews[].review.review.author.name` | 评论者昵称 |
| `reviews[].review.review.author.avatar` | 评论者头像 |
| `reviews[].review.review.book.bookId` | 书籍 ID |
| `reviews[].review.review.book.title` | 书名 |
| `reviews[].review.review.book.author` | 书籍作者 |

> 注意：返回结构中存在双层 `review` 嵌套。`reviews[].review` 是列表条目包装对象；点评内容字段（如 `content`、`htmlContent`、`star`、`author`、`book`）位于内层 `reviews[].review.review.xxx`。

## 工作流

1. 确定书籍：用户提供 bookId 直接使用，提供书名则先调 `/store/search` 获取 bookId。
2. 调 `/review/list` 获取公开点评列表。
   - 默认 `reviewListType=0` 看全部
   - 用户要看推荐的传 `reviewListType=1`
   - 用户要看最新的传 `reviewListType=3`
   - 用户要看差评的传 `reviewListType=2`
   - 用户要看一般的传 `reviewListType=4`
3. 每条点评展示：评论者昵称、评分星级、点评内容（长内容截取摘要）。
4. 翻页：用上一页最后一条的 `idx` 作为 `maxIdx`，带上 `synckey`。

## 输出格式
- 点评列表每条清晰分隔
- 评分转为星级展示（100=⭐⭐⭐⭐⭐，80=⭐⭐⭐⭐，60=⭐⭐⭐，40=⭐⭐，20=⭐）
- 长点评截取前 200 字，提示可展开
- 时间戳展示时转为 `YYYY-MM-DD`，不要直接展示原始 Unix 时间戳
- 无结果时回复：`这本书暂时还没有公开点评哦~`
- 接口调用失败时回复：`点评数据获取失败，请稍后再试~`
