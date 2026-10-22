"""Knowledge-base tool selection and evidence instructions."""

_BASE_TOOL_DESCRIPTION = """公有知识库检索：严格按平台选择的知识库后端检索。
自建模式只检索当前用户有权访问的本地公有/共享库；外部模式只检索所选 Dify、FastGPT 或 WeKnora 数据集。
两类来源不合并，也不会在空结果或失败时互相兜底。个人私有文档使用 retrieve_local_kb。

引用规则：引用返回内容时必须把条目自带的 `cite_id`（如 `e7`）原样写成 `[锚文本](cite:e7)` 标记，禁止自行编号。

适用场景（涉及以下内容时**主动**调用，无需用户显式要求）：政策文件原文/解读/申报条件、
产业分析报告、行业研究、发展规划、企业调研材料、经济运行分析等非结构化文本。

调用说明：**dataset_id 默认留空**（搜索当前后端允许的所有公有库），仅当用户指定某个知识库时才传。
回答时从本地结果的 `content` 或外部结果的 `segment -> content` 提取要点。

Args:
    query: 检索 query。
    dataset_id: 当前后端的知识库 ID（自建模式为 kb_ 前缀；默认空 = 搜当前后端允许的公有/共享库）。
    top_k: 返回片段数量。
    score_threshold: 相似度阈值。
    search_method: 检索方式（默认 hybrid_search）。
    reranking_enable: 是否启用重排。
    weights: 混合检索权重。

调用决策:
- 优先级高：政策/报告/规划类原文检索第一优先级。要"结构化数字"走指标类能力，要文段走我。
- 公有/共享资料用我，后端由平台配置决定；个人私有文档使用 retrieve_local_kb。
- 内部制度和业务记录优先对应内部权威来源；公开资料、最新信息和技术调研可直接使用 internet_search，并按需抓取原文核验。
"""


_LIST_DATASETS_DESCRIPTION = """列出当前可用的所有知识库（公有 + 私有），含名称、简介和文档列表。

适用场景：用户问"有哪些知识库/数据集"；或不确定该查哪个库时，先看列表再检索。

Returns:
    dict: {"public_datasets": [...], "private_datasets": [...], "total": N}
    public_datasets 使用 retrieve_dataset_content，按平台配置只搜索本地公有库或所选外部后端。
    private_datasets 使用 retrieve_local_kb。
    private_datasets 仅含当前用户自己的私有库；问"有几个公有库"以 public_datasets 为准，
    不要把本地公有库当私有库。
"""


_BASE_LOCAL_KB_TOOL_DESCRIPTION = """检索当前用户上传的个人私有知识库。
公有/共享资料必须使用 retrieve_dataset_content，由平台配置决定来源；不要使用本工具作为公有检索的替代或兜底。

引用规则：引用返回内容时必须把条目自带的 `cite_id`（如 `e7`）原样写成 `[锚文本](cite:e7)` 标记，禁止自行编号。

适用场景（**主动**调用，无需用户显式要求）：用户个人私有库中的项目材料、个人笔记或专属文档。

调用说明：先从 `list_datasets` 的 private_datasets 中选择明确的 kb_id（kb_ 前缀）。
不要将 public_datasets 的库传给本工具；公有库统一走 retrieve_dataset_content。
命中片段带 images 时，据其 caption（图的内容描述）作答并照常带 cite 标记，不要编造图中没有的数字。
**用户要求"看这张图 / 把图发出来"时，直接在回答里写 markdown 图片 `![](url)`**（url 原样复制），
对话区会把它渲染成图片——不需要、也不要绕去「我的空间」或沙盒找同名文件。

Args:
    kb_id: 当前用户选择的私有知识库 ID。
    query: 检索问题。
    top_k: 返回片段数量（默认 10）。

Returns:
    dict: {"available_kbs": [...], "items": [{"title","content","kb_id","score","images?":[{"asset_id","caption","url"}]}]}

调用决策: 仅在用户需要个人私有库资料时使用。
公有知识库检索使用 retrieve_dataset_content，按平台配置在自建公有库与外部数据集之间选择，来源不混用。
"""


_WIKI_OVERVIEW_DESCRIPTION = """查看某个知识库的**结构地图总览**：有哪些概念、规模多大、哪些是主干。

适用：用户问"这个知识库里有什么/涵盖哪些方面"；或不确定从哪查起时先看总览，再用
wiki_locate 定位。属"探路"工具，不直接产出答案（list_datasets 答"有哪些库"，我答
"某个库内部结构长什么样"）。

Args:
    dataset_id: 知识库 ID（留空自动选用）。
    limit: 返回枢纽概念数（默认 20）。

Returns:
    dict: {"total_pages", "pages_by_type", "total_links", "hub_pages": [...]}
"""


_WIKI_LOCATE_DESCRIPTION = """【第①步·定位】在知识库的**概念地图**上定位问题落在哪些概念/实体上。

按关键词命中概念/实体页，返回标题、摘要和关系数量——**不返回长正文**，只告诉你"该看哪里"。
三步用法：① wiki_locate 定位 → ② 需要全貌时 wiki_expand 展开 → ③ **必须** wiki_fetch_source
取回原文再作答。⚠️ summary 是模型二次加工的概述，不能直接当答案，一律以取回的原文为准。

命中为空时按顺序补救：换更书面的术语（"牌照"常对不上《运营资质证书》）→ 用正则交替
（`资质|牌照|证书`）→ 仍为空改用 retrieve_dataset_content 语义检索。

Args:
    query: 检索词，支持正则交替（如 `编制|员额`）。
    dataset_id: 知识库 ID（留空自动选用）。
    limit: 返回条数（默认 8）。

Returns:
    dict: {"pages": [{"slug","title","type","summary","related_count","source_doc_count"}]}

调用决策: 问"某概念/机构/制度是什么、和什么有关"且术语明确时先走我；口语化提问或
我命中为空时走 retrieve_dataset_content 语义召回。
"""


_WIKI_READ_PAGE_DESCRIPTION = """读取某个 Wiki 概念页的完整内容与关系（wiki_locate 之后的精读步骤）。

正文里 `[[slug|显示名]]` 是指向其他概念页的链接。⚠️ 正文是模型综合原文写的**概述**，
作答的事实依据应来自 wiki_fetch_source 取回的原文；只要事实和出处时可跳过我直接
wiki_fetch_source。

Args:
    slug: 页面标识，形如 `entity/example-city` 或 `concept/xin-yong`。
    dataset_id: 知识库 ID（留空自动选用）。

Returns:
    dict: {"title","type","content","related_pages","referenced_by","has_source"}
"""


_WIKI_EXPAND_DESCRIPTION = """【第②步·展开】沿概念间关系把与某概念相关的其他概念一次拉齐。

聚合型问题（"一共有几类""哪些""分别""彼此什么关系"）的关键一步——相似度取前 N 个
片段天然答不全，必须沿概念图找齐再逐个回原文核实。单点事实问题不需要我，locate 完
直接 fetch_source。truncated=true 表示邻域被截断，作答时应说明、别声称已穷尽。

Args:
    slug: 中心概念的 slug（来自 wiki_locate）。
    dataset_id: 知识库 ID（留空自动选用）。
    depth: 展开层数（默认 1，最大 3）。
    limit: 最多返回相关概念数（默认 30）。

Returns:
    dict: {"nodes": [...], "edges": [...], "truncated": bool}
"""


_WIKI_FETCH_SOURCE_DESCRIPTION = """【第③步·取原文】按 Wiki 页面记录的血缘坐标直接取回它所依据的**原始文档段落**（按 ID 直取，非再检索）。

走了 wiki_locate / wiki_expand 之后**必须**走我再作答——事实依据必须来自本工具返回
的原文，不要用 Wiki 概述代替。已定位到概念页用我；没定位到才用 retrieve_dataset_content
重新语义检索。

引用规则：引用返回内容时必须把条目自带的 `cite_id`（如 `e7`）原样写成 `[锚文本](cite:e7)` 标记，禁止自行编号。

Args:
    slug: 页面标识（来自 wiki_locate / wiki_expand）。
    dataset_id: 知识库 ID（留空自动选用）。
    max_chunks: 最多取回几段原文（默认 6）。

Returns:
    dict: {"wiki_page": "...", "items": [{"文件名称","文件内容","document_id","chunk_id"}]}
"""
