/** 2026-10-22 起有一段时间，MCP 工具结果以原始 CallToolResult（content /
 *  structuredContent / isError / _meta）推到前端并落库；后端现已改回推工具自己的值，
 *  这里兼容那批历史，换回卡片一直在渲染的形状：
 *
 *  - 文本块是完整 JSON：就用它（与当时后端的 json.loads(content) 一致）；
 *  - 文本被截断、解析不了：退回 structuredContent。SDK 会把非对象返回包成
 *    `{"result": …}`，落库的外壳里没有 schema 可查，所以只有唯一键 result、值是
 *    对象/数组时才拆这一层（字符串保持包着，与文本路径的 {result: text} 一致）；
 *  - 都没有：非 JSON 文本按 {result: text} 返回。其它形状原样返回。 */
export function unwrapMcpResult(value: unknown): unknown {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return value;
  const envelope = value as { content?: unknown; structuredContent?: unknown; isError?: unknown };
  if (!Array.isArray(envelope.content) || typeof envelope.isError !== 'boolean') return value;
  const text = envelope.content
    .filter((block): block is { type: 'text'; text: string } =>
      !!block && typeof block === 'object'
      && (block as { type?: unknown }).type === 'text'
      && typeof (block as { text?: unknown }).text === 'string')
    .map((block) => block.text)
    .join('');
  if (text) {
    try { return JSON.parse(text); } catch { /* truncated: fall back below */ }
  }
  const structured = envelope.structuredContent;
  if (structured != null && typeof structured === 'object' && !Array.isArray(structured)) {
    const keys = Object.keys(structured);
    const inner = (structured as { result?: unknown }).result;
    if (keys.length === 1 && keys[0] === 'result' && inner && typeof inner === 'object') return inner;
    return structured;
  }
  if (structured != null) return structured;
  return text ? { result: text } : value;
}
