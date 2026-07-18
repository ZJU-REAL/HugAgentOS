import type { UpdateEntry, CapItem } from './types';

// 社区版默认更新记录 / 能力清单（真源在 DB content_blocks，可经部署后配置覆盖）。
// 商业版历史更新记录与行业能力介绍不随社区版分发。

export const DEFAULT_FEATURE_UPDATES: UpdateEntry[] = [];

export const DEFAULT_CAPABILITIES_LIST: CapItem[] = [
  {
    title: '智能对话与深度研究',
    desc: '基于大模型的多轮对话、计划模式与深度研究流程，关键结论自动标注可点击的来源引用。',
    bullets: [
      'SSE 流式回复 · ReAct 工具编排 · Thinking 过程可视',
      '引用标注系统：结论可溯源、点击直达原文',
    ],
  },
  {
    title: '私有知识库检索（RAG）',
    desc: '上传文档自动分块向量化，向量 + BM25 混合检索与重排序，让回答基于你自己的资料。',
    bullets: [
      '支持 PDF / Word / Excel / 文本等常见格式',
      '可与互联网搜索协同，多来源交叉验证',
    ],
  },
  {
    title: '通用工具与自动化',
    desc: '内置网页抓取、互联网搜索、图表生成、报告导出、批量执行等通用工具，并支持定时任务自动化。',
    bullets: [
      '批量执行：对 Excel 行 / 多份文档批量运行同一任务',
      '自动化：定时触发既定提示词流程',
      '数据画布：表格结果可视化编辑',
    ],
  },
];
