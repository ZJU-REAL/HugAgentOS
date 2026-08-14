import { useState } from 'react';
import { Input, Segmented, Tooltip } from 'antd';
import { InfoCircleOutlined } from '@ant-design/icons';
import type { KBIndexMode, WikiGranularity } from '../../types';
import { t } from '../../i18n';

interface IndexModePickerProps {
  value: KBIndexMode[];
  onChange: (modes: KBIndexMode[]) => void;
  granularity: WikiGranularity;
  onGranularityChange: (granularity: WikiGranularity) => void;
  extractionInstructions: string;
  onExtractionInstructionsChange: (value: string) => void;
  contentInstructions: string;
  onContentInstructionsChange: (value: string) => void;
  disabled?: boolean;
}

const MODE_CARDS: Array<{
  mode: KBIndexMode;
  icon: string;
  title: string;
  tagline: string;
  detail: string;
}> = [
  {
    mode: 'rag',
    icon: '◎',
    title: '检索',
    tagline: '按语义找原文',
    detail: '向量 + 关键词混合检索。提问时按语义召回相关的原文片段，是问答的默认底座。',
  },
  {
    mode: 'wiki',
    icon: '◈',
    title: 'Wiki',
    tagline: '自动整理成知识地图',
    detail:
      '用大模型从原文抽出实体与概念，为每个条目生成带出处的页面并互相链接，可按目录树与概念图谱浏览。'
      + '生成在索引完成后异步进行，会消耗模型用量。',
  },
];

const GRANULARITY_OPTIONS: Array<{ value: WikiGranularity; label: string; hint: string }> = [
  { value: 'focused', label: '聚焦', hint: '每篇只抽 3-7 个核心主题。索引最干净，成本最低。' },
  { value: 'standard', label: '标准', hint: '核心主题 + 有独立段落展开的实体概念。推荐。' },
  { value: 'exhaustive', label: '穷举', hint: '见名即收，适合当术语表。条目最多，成本最高。' },
];

/**
 * 索引方式选择：检索 / Wiki，可多选，两者同选即 LLM-Wiki 知识库。
 *
 * 建库是个高频且轻量的动作，所以这里只留「两张卡 + 一个粒度」这一屏能扫完的信息量，
 * 把解释性文字收进 tooltip、把自定义要求收进折叠区——需要的人点得到，不需要的人不被打扰。
 */
export default function IndexModePicker({
  value,
  onChange,
  granularity,
  onGranularityChange,
  extractionInstructions,
  onExtractionInstructionsChange,
  contentInstructions,
  onContentInstructionsChange,
  disabled,
}: IndexModePickerProps) {
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const hasWiki = value.includes('wiki');
  const isLlmWiki = value.includes('rag') && hasWiki;

  const toggle = (mode: KBIndexMode) => {
    if (disabled) return;
    const next = value.includes(mode) ? value.filter((m) => m !== mode) : [...value, mode];
    // 一个都不选没有意义（既不检索也不生成），兜底回检索
    onChange(next.length ? next : ['rag']);
  };

  return (
    <div className="jx-idxMode">
      <div className="jx-idxModeHead">
        <span className="jx-kbEditorLabel">{t('索引方式')}</span>
        {isLlmWiki && <span className="jx-idxModeBadge">{t('LLM-Wiki 知识库')}</span>}
      </div>

      <div className="jx-idxModeCards">
        {MODE_CARDS.map((card) => {
          const active = value.includes(card.mode);
          return (
            <button
              key={card.mode}
              type="button"
              role="checkbox"
              aria-checked={active}
              disabled={disabled}
              className={`jx-idxModeCard${active ? ' is-active' : ''}`}
              onClick={() => toggle(card.mode)}
            >
              <span className="jx-idxModeIcon">{card.icon}</span>
              <span className="jx-idxModeText">
                <span className="jx-idxModeTitle">
                  {t(card.title)}
                  <Tooltip title={t(card.detail)}>
                    <InfoCircleOutlined
                      className="jx-idxModeInfo"
                      onClick={(e) => e.stopPropagation()}
                    />
                  </Tooltip>
                </span>
                <span className="jx-idxModeTagline">{t(card.tagline)}</span>
              </span>
            </button>
          );
        })}
      </div>

      {hasWiki && (
        <div className="jx-idxModeWiki">
          <div className="jx-idxModeRow">
            <span className="jx-idxModeRowLabel">{t('抽取粒度')}</span>
            <Segmented
              size="small"
              value={granularity}
              disabled={disabled}
              onChange={(v) => onGranularityChange(v as WikiGranularity)}
              options={GRANULARITY_OPTIONS.map((o) => ({
                value: o.value,
                label: <Tooltip title={t(o.hint)}>{t(o.label)}</Tooltip>,
              }))}
            />
          </div>

          <button
            type="button"
            className="jx-idxModeToggle"
            onClick={() => setAdvancedOpen((v) => !v)}
          >
            {advancedOpen ? '▾' : '▸'} {t('自定义抽取要求')}
            <span className="jx-idxModeOptional">{t('选填')}</span>
          </button>

          {advancedOpen && (
            <div className="jx-idxModeAdvanced">
              <div className="jx-idxModeField">
                <label>{t('抽取哪些内容')}</label>
                <Input.TextArea
                  value={extractionInstructions}
                  onChange={(e) => onExtractionInstructionsChange(e.target.value)}
                  placeholder={t('例：重点关注补助金额、申报条件与责任部门，忽略排版说明')}
                  autoSize={{ minRows: 2, maxRows: 4 }}
                  maxLength={4000}
                  disabled={disabled}
                />
              </div>
              <div className="jx-idxModeField">
                <label>{t('条目页怎么写')}</label>
                <Input.TextArea
                  value={contentInstructions}
                  onChange={(e) => onContentInstructionsChange(e.target.value)}
                  placeholder={t('例：每页开头先给一句话结论，正文用条款式短句')}
                  autoSize={{ minRows: 2, maxRows: 4 }}
                  maxLength={4000}
                  disabled={disabled}
                />
              </div>
              <p className="jx-idxModeNote">
                {t('这些要求只影响抽什么、怎么写；出处标注与事实接地由系统保证，不受影响。')}
              </p>
            </div>
          )}
        </div>
      )}

      {hasWiki && !value.includes('rag') && (
        <p className="jx-idxModeNote">
          {t('仅 Wiki：文档照常分块（出处回溯依赖分块），但不建向量索引，该库不参与语义检索。')}
        </p>
      )}
    </div>
  );
}
