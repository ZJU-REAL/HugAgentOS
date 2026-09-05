import { useCallback, useMemo, useRef, useState } from 'react';
import { CloseOutlined, CompressOutlined, ReloadOutlined } from '@ant-design/icons';
import { Button, Empty } from 'antd';
import { ConceptGraph, type ConceptGraphHandle } from '../kb/ConceptGraph';
import { t } from '../../i18n';
import type { MemoryGraphRelation, WikiGraphNode } from '../../types';
import { buildEntityStats, buildGraphData } from './memoryGraphModel';
import {
  MEMORY_ROLE_STYLE,
  memoryRoleStyleOf,
  type MemoryEntityRole,
} from './memoryGraphTheme';

/**
 * L3 图谱记忆的图形视图：实体是点、关系是带名字的边。
 *
 * 之前这里是一行行「源 — 关系 → 目标」的文本，逐条读得懂、整体看不出结构——
 * 而 L3 的价值恰恰在结构：谁是枢纽、哪些实体串在一起。所以直接复用知识库概念
 * 图谱的那张力导向图（ConceptGraph），交互与配色都沿用同一套，用户在两处看到
 * 的是同一种图。
 *
 * 记忆实体没有后端给的类型，按「在关系里担任的角色」着色，见 memoryGraphTheme。
 * 点节点从右侧滑出抽屉，把它参与的每一条关系原样列出来——图看结构，抽屉看原文，
 * 原来列表能读到的信息一条不少。
 */

interface MemoryGraphViewProps {
  relations: MemoryGraphRelation[];
  enabled: boolean;
  onReload?: () => void;
}

export function MemoryGraphView({ relations, enabled, onReload }: MemoryGraphViewProps) {
  const [selected, setSelected] = useState<string>('');
  const [hiddenRoles, setHiddenRoles] = useState<Set<string>>(new Set());
  const graphRef = useRef<ConceptGraphHandle>(null);

  const entities = useMemo(() => buildEntityStats(relations), [relations]);

  const graph = useMemo(
    () => buildGraphData(entities, relations, hiddenRoles),
    [entities, relations, hiddenRoles],
  );

  /** 选中实体参与的全部关系，抽屉里按「它作为主语 / 作为宾语」原样列出 */
  const selectedRelations = useMemo(
    () => relations.filter((r) => r.source === selected || r.target === selected),
    [relations, selected],
  );

  const rolesPresent = useMemo(() => {
    const seen = new Set<MemoryEntityRole>();
    for (const e of entities.values()) seen.add(e.role);
    return (Object.keys(MEMORY_ROLE_STYLE) as MemoryEntityRole[]).filter((r) => seen.has(r));
  }, [entities]);

  const toggleRole = useCallback((role: string) => {
    setHiddenRoles((prev) => {
      const next = new Set(prev);
      if (next.has(role)) next.delete(role);
      else next.add(role);
      return next;
    });
  }, []);

  const onSelectNode = useCallback((node: WikiGraphNode) => setSelected(node.slug), []);
  const clearSelect = useCallback(() => setSelected(''), []);

  if (!enabled) {
    return (
      <Empty
        className="jx-anim-fadeIn"
        description={t('图谱记忆未启用（需配置 MEM0_GRAPH_ENABLED + Neo4j）')}
      />
    );
  }
  if (!relations.length) {
    return <Empty className="jx-anim-fadeIn" description={t('暂无实体关系')} />;
  }

  const selectedStat = selected ? entities.get(selected) : undefined;

  return (
    <div className="jx-memGraphWrap">
      <div className="jx-memGraphToolbar">
        <span className="jx-memGraphToolbarLabel">
          {t('{entities} 个实体 · {relations} 条关系', {
            entities: entities.size,
            relations: relations.length,
          })}
        </span>
        <Button
          type="text"
          size="small"
          icon={<CompressOutlined />}
          onClick={() => graphRef.current?.fitToView()}
        >
          {t('适应屏幕')}
        </Button>
        {onReload && (
          <Button type="text" size="small" icon={<ReloadOutlined />} onClick={onReload}>
            {t('刷新')}
          </Button>
        )}
      </div>

      <div className="jx-memGraphStage">
        {graph.nodes.length ? (
          <ConceptGraph
            ref={graphRef}
            data={graph}
            selectedSlug={selected || undefined}
            styleOf={memoryRoleStyleOf}
            showEdgeLabels
            onSelectNode={onSelectNode}
            onClearSelect={clearSelect}
          />
        ) : (
          <div className="jx-memGraphEmpty">{t('当前筛选下没有实体')}</div>
        )}

        <div className="jx-memGraphLegend">
          {rolesPresent.map((role) => {
            const style = MEMORY_ROLE_STYLE[role];
            const off = hiddenRoles.has(role);
            return (
              <button
                key={role}
                type="button"
                className={`jx-memGraphLegendItem${off ? ' is-off' : ''}`}
                onClick={() => toggleRole(role)}
                title={off ? t('点击显示') : t('点击隐藏')}
              >
                <i style={{ background: style.fill, borderColor: style.stroke }} />
                {t(style.label)}
              </button>
            );
          })}
          <span className="jx-memGraphLegendHint">{t('拖拽节点 · 滚轮缩放 · 单击看关系')}</span>
        </div>

        {selectedStat && (
          <aside className="jx-memGraphDrawer">
            <div className="jx-memGraphDrawerHead">
              <i
                style={{
                  background: memoryRoleStyleOf(selectedStat.role).fill,
                  borderColor: memoryRoleStyleOf(selectedStat.role).stroke,
                }}
              />
              <h4 title={selectedStat.name}>{selectedStat.name}</h4>
              <Button
                type="text"
                size="small"
                icon={<CloseOutlined />}
                aria-label={t('关闭')}
                onClick={clearSelect}
              />
            </div>
            <div className="jx-memGraphDrawerMeta">
              {t('{role} · 参与 {n} 条关系', {
                role: t(MEMORY_ROLE_STYLE[selectedStat.role].label),
                n: selectedRelations.length,
              })}
            </div>
            <div className="jx-memGraphDrawerBody">
              {selectedRelations.map((r, i) => (
                <div className="jx-memGraphTriple" key={`${r.source}-${r.relationship}-${r.target}-${i}`}>
                  <button
                    type="button"
                    className={`jx-memGraphTripleEnd${r.source === selected ? ' is-self' : ''}`}
                    onClick={() => setSelected(r.source)}
                  >
                    {r.source}
                  </button>
                  <span className="jx-memGraphTripleRel">{r.relationship}</span>
                  <button
                    type="button"
                    className={`jx-memGraphTripleEnd${r.target === selected ? ' is-self' : ''}`}
                    onClick={() => setSelected(r.target)}
                  >
                    {r.target}
                  </button>
                </div>
              ))}
            </div>
          </aside>
        )}
      </div>
    </div>
  );
}

export default MemoryGraphView;
