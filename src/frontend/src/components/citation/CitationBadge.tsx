import { Popover } from 'antd';
import type { CitationItem } from '../../types';
import { t } from '../../i18n';

const CITATION_ICON: Record<string, string> = {
  internet:       '/icons/internet.png',
  knowledge_base: '/icons/knowledge.png',
  database:       '/icons/database.png',
  industry_news:  '/icons/news.png',
  ai_news:        '/icons/ai-news.png',
  chain_info:        '/icons/industry-chain.png',
  company_profile:   '/icons/industry-chain.png',
};

const CITATION_LABEL: Record<string, string> = {
  internet:       t('互联网'),
  knowledge_base: t('知识库'),
  database:       t('数据库'),
  industry_news:  t('产业资讯'),
  ai_news:        t('AI 动态'),
  chain_info:        t('产业链'),
  company_profile:   t('企业画像'),
};

export { CITATION_ICON, CITATION_LABEL };

export default function CitationBadge({
  citId,
  citations,
  onCitationAction,
}: {
  citId: string;
  citations: CitationItem[];
  onCitationAction?: (citation: CitationItem) => void;
}) {
  const cit = citations.find(c => c.id === citId);
  const iconPath = cit ? (CITATION_ICON[cit.source_type] || null) : null;
  const label = cit ? (CITATION_LABEL[cit.source_type] || t('来源')) : t('来源');
  const iconEl = (size: number) => iconPath
    ? <img src={iconPath} alt={label} style={{ width: size, height: size, verticalAlign: 'middle', objectFit: 'contain' }} />
    : <span style={{ fontSize: size }}>📄</span>;
  const indexPart = citId.split('-').pop() || '';
  const isInternet = cit?.source_type === 'internet';
  const canOpenDetail = !!cit && !!onCitationAction;
  const openDetail = () => {
    if (!cit || !onCitationAction) return;
    onCitationAction(cit);
  };

  const hoverContent = cit ? (
    <div
      style={{ maxWidth: 300, fontSize: 13, cursor: canOpenDetail ? 'pointer' : 'default' }}
      role={canOpenDetail ? 'button' : undefined}
      tabIndex={canOpenDetail ? 0 : undefined}
      onClick={canOpenDetail ? openDetail : undefined}
      onKeyDown={canOpenDetail ? (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          openDetail();
        }
      } : undefined}
      title={canOpenDetail ? (isInternet ? t('点击打开原文链接') : t('点击查看全文')) : undefined}
    >
      <div style={{ fontWeight: 600, marginBottom: 4, color: '#808080', fontSize: 12, display: 'flex', alignItems: 'center', gap: 4 }}>
        {iconEl(14)} {label}
      </div>
      <div style={{ marginBottom: cit.snippet ? 6 : 0, fontWeight: 600, color: isInternet ? '#126DFF' : '#262626' }}>
        {cit.title}
      </div>
      {cit.snippet && (
        <div style={{ fontSize: 12, color: '#808080', lineHeight: 1.6, borderLeft: '3px solid #DBE9FF', paddingLeft: 8 }}>
          {cit.snippet.length > 160 ? cit.snippet.slice(0, 160) + '…' : cit.snippet}
        </div>
      )}
      {isInternet && cit.url && (
        <div style={{ marginTop: 6, fontSize: 11, color: '#B3B3B3' }}>
          🔗 {(() => { try { return new URL(cit.url).hostname; } catch { return cit.url.slice(0, 40); } })()}
        </div>
      )}
      {(cit.snippet || cit.url) && (
        <div style={{ marginTop: 6, fontSize: 11, color: '#126DFF' }}>
          {isInternet ? t('点击此卡片打开原文 →') : t('点击此卡片查看全文 →')}
        </div>
      )}
    </div>
  ) : (
    <div style={{ color: '#B3B3B3', fontSize: 12 }}>{t('引用 {citId} 未找到', { citId })}</div>
  );

  // Styles live in common.css's .jx-citBadge — the badge sits on the markdown portal + DOM transplant path,
  // so motion components must never be used (remount flicker); all hover feedback goes through CSS.
  const badgeEl = (
    <sup className="jx-citBadge">
      {iconEl(11)}{indexPart}
    </sup>
  );

  return (
    <Popover
      content={hoverContent}
      trigger="hover"
      placement="top"
      // During streaming output, the cursor sweeping across the text no longer causes cascading popover flickers
      mouseEnterDelay={0.15}
      overlayStyle={{ zIndex: 9999 }}
    >
      {badgeEl}
    </Popover>
  );
}
