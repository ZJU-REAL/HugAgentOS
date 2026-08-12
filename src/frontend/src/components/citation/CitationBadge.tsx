import { Popover } from 'antd';
import type { CitationItem } from '../../types';
import { t } from '../../i18n';

const CITATION_ICON: Record<string, string> = {
  internet:       '/icons/internet.png',
  knowledge_base: '/icons/knowledge.png',
  database:       '/icons/database.png',
};

const CITATION_LABEL: Record<string, string> = {
  internet:       t('互联网'),
  knowledge_base: t('知识库'),
  database:       t('数据库'),
};

export { CITATION_ICON, CITATION_LABEL };

export default function CitationBadge({
  citId,
  citations,
  onCitationAction,
  anchorLabel,
}: {
  citId: string;
  citations: CitationItem[];
  onCitationAction?: (citation: CitationItem) => void;
  /** 锚文本（[锚文本](cite:eN) 写法）；历史消息的旧标记没有锚文本，退化为来源标题 */
  anchorLabel?: string;
}) {
  const cit = citations.find(c => c.id === citId);
  const iconPath = cit ? (CITATION_ICON[cit.source_type] || null) : null;
  const label = cit ? (CITATION_LABEL[cit.source_type] || t('来源')) : t('来源');
  const iconEl = (size: number) => iconPath
    ? <img src={iconPath} alt={label} style={{ width: size, height: size, verticalAlign: 'middle', objectFit: 'contain' }} />
    : <span style={{ fontSize: size }}>📄</span>;
  // chip 内的来源小图标（无图标的来源类型不占位，避免出现空白方块）
  const chipIcon = iconPath
    ? <img className="jx-citeRef-icon" src={iconPath} alt="" aria-hidden />
    : null;
  const isInternet = cit?.source_type === 'internet';
  const canOpenDetail = !!cit && !!onCitationAction;
  const openDetail = () => {
    if (!cit || !onCitationAction) return;
    onCitationAction(cit);
  };

  const domain = (() => {
    if (!cit?.url) return '';
    try { return new URL(cit.url).hostname.replace(/^www\./, ''); } catch { return cit.url.slice(0, 40); }
  })();

  // 没有锚文本时（历史 [ref:] 标记、或模型写成 [来源](cite:eN)）用来源标题占位，
  // 截断以免整条标题撑在正文中间
  const fallbackLabel = (() => {
    const title = (cit?.title || '').trim();
    if (!title) return label;
    return title.length > 14 ? `${title.slice(0, 14)}…` : title;
  })();

  const hoverContent = cit ? (
    <div
      className="jx-citCard"
      role={canOpenDetail ? 'button' : undefined}
      tabIndex={canOpenDetail ? 0 : undefined}
      onClick={canOpenDetail ? openDetail : undefined}
      onKeyDown={canOpenDetail ? (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          openDetail();
        }
      } : undefined}
    >
      <div className="jx-citCard-head">
        {iconEl(13)}
        <span className="jx-citCard-source">{label}</span>
        <span className="jx-citCard-anchor">{citId}</span>
      </div>
      <div className="jx-citCard-title">{cit.title}</div>
      {cit.snippet && (
        <div className="jx-citCard-snippet">
          {cit.snippet.length > 180 ? cit.snippet.slice(0, 180) + '…' : cit.snippet}
        </div>
      )}
      {(domain || canOpenDetail) && (
        <div className="jx-citCard-foot">
          {domain && <span className="jx-citCard-domain">{domain}</span>}
          {canOpenDetail && (
            <span className="jx-citCard-action">
              {isInternet && cit.url ? t('打开原文 →') : t('查看全文 →')}
            </span>
          )}
        </div>
      )}
    </div>
  ) : (
    <div className="jx-citCard jx-citCard--missing">{t('引用 {citId} 未找到', { citId })}</div>
  );

  // Styles live in common.css (.jx-citeRef / .jx-citCard) — these sit on the markdown portal +
  // DOM transplant path, so motion components must never be used (remount flicker); all hover
  // feedback goes through CSS.
  //
  // 内联锚点刻意**不做成超链接样式**：`cite:eN` 不是可跳转地址，蓝字+下划线会诱导用户
  // 期待跳转。改成"选中态"观感——灰色实心底 + 加粗，被引用的文字一眼可辨。
  // 全局只有这一种形态：历史消息的旧标记没有锚文本，退化为来源标题。
  const badgeEl = (
    <span
      className="jx-citeRef"
      role="button"
      tabIndex={0}
      aria-label={cit ? `${label}：${cit.title}` : citId}
      onClick={canOpenDetail ? openDetail : undefined}
      onKeyDown={canOpenDetail ? (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          openDetail();
        }
      } : undefined}
    >
      {chipIcon}
      {anchorLabel || fallbackLabel}
    </span>
  );

  return (
    <Popover
      content={hoverContent}
      trigger="hover"
      // 优先在上方展示；上方空间不够时 antd 的 autoAdjustOverflow 会自动翻到下方
      placement="top"
      arrow={{ pointAtCenter: true }}
      // During streaming output, the cursor sweeping across the text no longer causes cascading popover flickers
      mouseEnterDelay={0.15}
      mouseLeaveDelay={0.1}
      overlayClassName="jx-citPopover"
      overlayStyle={{ zIndex: 9999 }}
    >
      {badgeEl}
    </Popover>
  );
}
