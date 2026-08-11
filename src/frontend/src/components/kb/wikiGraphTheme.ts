/**
 * 概念图谱的类型配色。
 *
 * SVG 里没法用 CSS 变量，只能给具体色值；图（ConceptGraph）、图例与右侧详情面板
 * 三处都要用同一份，所以单独成文件——放进组件文件会破坏 fast refresh。
 * 色相沿用 WeKnora 自身的分类语义，避免两边看同一个库时对不上号。
 */
export interface GraphTypeStyle {
  fill: string;
  stroke: string;
  label: string;
}

export const GRAPH_TYPE_STYLE: Record<string, GraphTypeStyle> = {
  summary: { fill: '#DBE9FF', stroke: '#0052D9', label: '文档摘要' },
  entity: { fill: '#DEF3E9', stroke: '#2BA471', label: '实体' },
  concept: { fill: '#FDEBDA', stroke: '#E37318', label: '概念' },
  synthesis: { fill: '#DAEEFE', stroke: '#0594FA', label: '综述' },
  comparison: { fill: '#FBE0DE', stroke: '#D54941', label: '对比' },
  index: { fill: '#EDE9FE', stroke: '#7C5CFC', label: '索引' },
};

export const GRAPH_FALLBACK_STYLE: GraphTypeStyle = {
  fill: '#F5F6F7',
  stroke: '#808080',
  label: '其他',
};

export function graphStyleOf(pageType: string): GraphTypeStyle {
  return GRAPH_TYPE_STYLE[pageType] || GRAPH_FALLBACK_STYLE;
}
