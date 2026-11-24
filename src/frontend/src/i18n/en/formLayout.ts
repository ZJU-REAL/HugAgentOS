/** 英文字典（紧凑表单 / 弹窗重排域）：FormLayout、模型供应商、知识库索引字段、对话模式字段等共享组件新增文案。 */
export const FORM_LAYOUT_DICT: Record<string, string> = {
  // ── components/common/FormLayout ──
  '高级设置': 'Advanced settings',
  // ── 模型供应商（ModelsEditor / SystemModelPanel） ──
  '上下文长度（token）': 'Context length (tokens)',
  '探测': 'Detect',
  '思考走独立通道': 'Separate reasoning channel',
  '多档思考强度': 'Multiple reasoning levels',
  '切换权重': 'Failover weight',
  // ── components/common/ChatModeFormFields ──
  '从市场选择': 'Pick from market',
  // ── components/kb/IndexingConfigFields ──
  '块大小、自动关键词、分隔符': 'Chunk size, auto keywords, separators',
  '自动关键词数': 'Auto keywords',
  '自动问题数': 'Auto questions',
  '0 = 关闭': '0 = off',
  '父分块分隔符': 'Parent chunk separators',
  '子分块分隔符': 'Child chunk separators',
  '自定义分隔符': 'Custom separators',
  '每行一个，留空用默认。「递归分块」以此为分块依据（相邻小片段会合并到父块大小）；语义分块仅超长时用它兜底。可用 \\n \\t 表示换行/制表符':
    'One per line; leave empty for defaults. Recursive chunking splits on these (adjacent small pieces merge up to the parent size); semantic chunking only falls back to them for oversized pieces. Use \\n \\t for newline/tab.',
  '每行一个，留空用默认。父块拆成子块时按这些分隔符切，再按子块大小打包；为空走定长滑窗。可用 \\n \\t 表示换行/制表符':
    'One per line; leave empty for defaults. Parent chunks are split into child chunks on these separators, then packed to the child size; empty uses a fixed-size sliding window. Use \\n \\t for newline/tab.',
};
