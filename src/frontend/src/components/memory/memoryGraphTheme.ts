import {
  GRAPH_FALLBACK_STYLE,
  GRAPH_TYPE_STYLE,
  type GraphTypeStyle,
} from '../kb/wikiGraphTheme';

/**
 * L3 实体关系图谱的配色。
 *
 * 记忆层的实体没有后端给定的类型，所以不按名字去猜类别，而是按实体在关系里的
 * **角色**着色——这是关系数据本身就能算出来的事实，新实体进来会自动落到正确的
 * 一档，不需要维护任何名单。
 *
 * 色值直接取知识库概念图谱那份 GRAPH_TYPE_STYLE，只换标签文案：两处图谱共用一套
 * 色相，用户看哪边都对得上号，调色板也只有一个地方要维护。
 */
export type MemoryEntityRole = 'hub' | 'subject' | 'object';

export const MEMORY_ROLE_STYLE: Record<MemoryEntityRole, GraphTypeStyle> = {
  hub: { ...GRAPH_TYPE_STYLE.summary, label: '枢纽实体' },
  subject: { ...GRAPH_TYPE_STYLE.entity, label: '主体' },
  object: { ...GRAPH_TYPE_STYLE.concept, label: '关联对象' },
};

export function memoryRoleStyleOf(role: string): GraphTypeStyle {
  return MEMORY_ROLE_STYLE[role as MemoryEntityRole] || GRAPH_FALLBACK_STYLE;
}

/** 既发出又承接关系的是枢纽，只发出的是主体，只承接的是关联对象 */
export function roleOf(outgoing: number, incoming: number): MemoryEntityRole {
  if (outgoing > 0 && incoming > 0) return 'hub';
  return outgoing > 0 ? 'subject' : 'object';
}
