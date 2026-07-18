/** 「元数据管理」域英文字典（数据库直连取数的表/列语义 + 枚举字典 + 黄金 SQL）。 */
export const DB_METADATA_DICT: Record<string, string> = {
  '元数据': 'Metadata',
  '元数据管理': 'Metadata',
  '元数据管理 · {name}': 'Metadata · {name}',
  '数据源': 'Data source',
  '选择直连数据源': 'Select a direct-connect data source',
  '一键探查 schema': 'Introspect schema',
  '标注表/字段语义、枚举字典与黄金 SQL，提升取数准确率':
    'Annotate table/field semantics, enum dictionaries and golden SQL to improve query accuracy',
  '为「数据库直连查询」标注表/字段业务含义、枚举字典（如 status=1→已审核）与已验证的查询范例。取数时智能体会通过 get_data_context 工具按需读取这些标注来更准确地写 SQL——这些内容不会写进系统提示词。外部智能取数（NL2SQL）自带语义、不在此治理范围。':
    'Annotate table/field business meaning, enum dictionaries (e.g. status=1→Approved) and verified query examples for "Direct DB Query". At query time the agent reads these on demand via the get_data_context tool to write SQL more accurately — none of this goes into the system prompt. External NL2SQL carries its own semantics and is out of scope here.',
  '加载元数据失败：{msg}': 'Failed to load metadata: {msg}',
  '加载数据源失败：{msg}': 'Failed to load data sources: {msg}',
  '探查完成：新增 {tn} 张表 / {cn} 个字段': 'Introspection done: added {tn} tables / {cn} fields',
  '探查未成功：{msg}（可手动添加表/列）':
    'Introspection unsuccessful: {msg} (you can add tables/columns manually)',
  '探查失败：{msg}': 'Introspection failed: {msg}',
  '暂无直连数据源。请先到「数据库工具」添加 MySQL/PostgreSQL 等直连数据源。':
    'No direct-connect data source yet. Add a MySQL/PostgreSQL source under "Database Tools" first.',

  // 表与列
  '表与列（{a}/{b} 表已标注）': 'Tables & columns ({a}/{b} annotated)',
  '黄金 SQL（{n}）': 'Golden SQL ({n})',
  '表': 'Table',
  '字段数': 'Fields',
  '描述/口径': 'Description / definition',
  '加字段': 'Add field',
  '新增表': 'Add table',
  '新增字段': 'Add field',
  '新增范例': 'Add example',
  '编辑表': 'Edit table',
  '编辑字段': 'Edit field',
  '编辑范例': 'Edit example',
  '尚无字段标注，点上方「加字段」添加': 'No field annotations yet. Click "Add field" above.',
  '删除该表及其字段标注？': 'Delete this table and its column annotations?',
  '确认删除该字段标注？': 'Delete this field annotation?',

  // 字段
  '业务名': 'Business name',
  '主键': 'Primary key',
  '类型/角色': 'Type / role',
  '枚举字典': 'Enum dictionary',
  '枚举字典（每行一条 code=含义，如 1=已审核）':
    'Enum dictionary (one per line, code=meaning, e.g. 1=Approved)',
  '字段取值是编码时务必填写——这是提升取数准确率最关键的一项。':
    'Fill this in when the column stores codes — the single most important lever for query accuracy.',
  '{n} 项': '{n} items',
  '语义角色': 'Semantic role',
  '生命周期': 'Lifecycle',
  '样例值（逗号分隔）': 'Sample values (comma-separated)',
  '外键（other_table.column）': 'Foreign key (other_table.column)',
  '同义词（逗号分隔）': 'Synonyms (comma-separated)',
  '敏感信息(PII)': 'Sensitive (PII)',
  '纳入数据字典': 'Include in dictionary',
  '不入字典': 'Excluded',
  '字段名（物理）': 'Column name (physical)',
  '表名（物理）': 'Table name (physical)',
  'schema（可选）': 'Schema (optional)',

  // 语义角色 / 生命周期 选项
  '未设': 'Unset',
  '维度': 'Dimension',
  '度量': 'Measure',
  '已认证': 'Certified',
  '已废弃': 'Deprecated',
  '已废弃（别用）': 'Deprecated (do not use)',

  // 黄金 SQL
  '经人工核对正确的「问题→SQL」范例，是提升取数准确率最有效的手段。':
    'Human-verified question→SQL examples are the most effective way to improve query accuracy.',
  '问题': 'Question',
  '问题（自然语言）': 'Question (natural language)',
  '已验证': 'Verified',
  '待验证': 'Unverified',
  '请输入问题': 'Enter the question',
  '请输入 SQL': 'Enter SQL',
  '请输入表名': 'Enter table name',
  '请输入字段名': 'Enter column name',

  // 占位示例
  '订单主表': 'Orders table',
  '订单状态': 'Order status',
  '订单表, orders': 'orders table, orders',
  '状态, 审核状态': 'status, review status',
  '每行一笔订单……': 'One order per row…',
  '近7天已审核的订单总数': 'Total approved orders in the last 7 days',
};
