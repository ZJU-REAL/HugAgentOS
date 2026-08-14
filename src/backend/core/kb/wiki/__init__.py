"""自建知识库的 LLM Wiki：生成管线与读取面。

管线是一条 Map-Reduce：

    Pass 0  候选抽取   逐文档抽出实体/概念的 slug 骨架
    Taxonomy 目录规划  整批一次性分配目录，保证目录树连贯
    Pass 1..N 引文标注 逐批分块标注「哪几块讨论了这个 slug」
    Dedup   去重合并   新条目与既有页是否同指一物
    Reduce  写页       按 slug 增量合并/新建页面
    Finalize 收尾      索引页 / 死链 / 反向链接 / 空目录，纯 SQL 无 LLM

设计上有三条不能动摇的原则：

1. **引文不让模型转述。** Pass 1 只让模型回答「哪几个 chunk 讨论了它」，事实以
   chunk 原文逐字进入 Reduce。这是 Wiki 不跑偏的根本机制。
2. **Reduce 阶段模型是编译器不是作者。** 贴着原文措辞走，不许润色扩写。
3. **Wiki 是地图不是第二检索源。** 答案与出处始终回到 kb_chunks 的原文。
"""

from core.kb.wiki.config import WikiConfig, resolve_wiki_config, wiki_enabled

__all__ = ["WikiConfig", "resolve_wiki_config", "wiki_enabled"]
