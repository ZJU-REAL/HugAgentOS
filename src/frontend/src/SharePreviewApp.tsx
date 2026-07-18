import { useEffect, useMemo, useState } from 'react';
import CitationMarkdownBlock from './components/citation/CitationMarkdownBlock';
import { PlanCard } from './components/chat/PlanCard';
import type { PlanCardProps } from './components/chat/PlanCard';
import { formatDateTime } from './utils/date';
import { staggerStyle } from './utils/motionTokens';
import { usePageConfig, usePageConfigPolling } from './hooks/usePageConfig';
import { t } from './i18n';

type ShareMessageItem = {
  role: 'user' | 'assistant';
  content: string;
  is_markdown?: boolean;
  created_at?: string | null;
  plan_data?: Omit<PlanCardProps, 'isStreaming'> | null;
};

type SharePayload = {
  share_id: string;
  title: string;
  created_at: string;
  expires_at?: string | null;
  expiry_option?: '3d' | '15d' | '3m' | 'permanent';
  created_by_username?: string;
  items: ShareMessageItem[];
};

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string || '').trim() || '/api';

function formatShareExpiry(value?: string | null) {
  if (!value) return t('长期');
  return formatDateTime(value, '');
}

export default function SharePreviewApp() {
  usePageConfigPolling();
  const brandName = usePageConfig('branding.product_name', 'HugAgentOS');

  const shareId = useMemo(() => new URLSearchParams(window.location.search).get('share') || '', []);
  const [loading, setLoading] = useState(true);
  const [expired, setExpired] = useState(false);
  const [payload, setPayload] = useState<SharePayload | null>(null);
  const visibleShareTitle = useMemo(() => {
    const rawTitle = payload?.title?.trim();
    return rawTitle || t('未命名分享');
  }, [payload?.title]);
  const sharePageTitle = useMemo(() => {
    return `${brandName}${t('会话分享：')}${visibleShareTitle}`;
  }, [brandName, visibleShareTitle]);

  useEffect(() => {
    if (typeof document === 'undefined') return;
    if (loading) {
      document.title = `${brandName}${t('会话分享')}`;
      return;
    }
    if (expired || !payload) {
      document.title = `${brandName}${t('会话分享：链接已失效')}`;
      return;
    }
    document.title = sharePageTitle;
  }, [loading, expired, payload, sharePageTitle, brandName]);

  useEffect(() => {
    if (!shareId) {
      setExpired(true);
      setLoading(false);
      return;
    }

    let active = true;
    const load = async () => {
      try {
        const response = await fetch(`${API_BASE}/v1/chat-shares/${shareId}`);
        if (!response.ok) {
          if (active) {
            setExpired(true);
            setLoading(false);
          }
          return;
        }
        const result = await response.json();
        if (!active) return;
        setPayload(result?.data || null);
      } catch {
        if (active) setExpired(true);
      } finally {
        if (active) setLoading(false);
      }
    };

    void load();
    return () => {
      active = false;
    };
  }, [shareId]);

  if (loading) {
    return (
      <div className="jx-sharePage">
        <div className="jx-shareCard jx-shareCardSkeleton" aria-hidden="true">
          <div className="jx-shareHeader">
            <div className="jx-shareHeaderTop">
              <div className="jx-skeletonBlock jx-sharePreviewSkEyebrow" />
              <div className="jx-skeletonBlock jx-sharePreviewSkPrint" />
            </div>
            <div className="jx-skeletonBlock jx-sharePreviewSkTitle" />
            <div className="jx-shareMeta">
              <div className="jx-skeletonBlock jx-sharePreviewSkMeta" />
              <div className="jx-skeletonBlock jx-sharePreviewSkMeta" />
              <div className="jx-skeletonBlock jx-sharePreviewSkMeta jx-sharePreviewSkMetaWide" />
            </div>
          </div>
          <div className="jx-shareMessages">
            {[0, 1, 2, 3, 4].map((item) => (
              <div key={item} className="jx-shareMessage jx-shareMessageSkeleton">
                <div className="jx-shareMessageHeader">
                  <div className="jx-skeletonBlock jx-sharePreviewSkLabel" />
                  <div className="jx-skeletonBlock jx-sharePreviewSkTime" />
                </div>
                <div className="jx-sharePreviewSkBody">
                  <div className="jx-skeletonBlock jx-sharePreviewSkLine jx-sharePreviewSkLineLong" />
                  <div className="jx-skeletonBlock jx-sharePreviewSkLine" />
                  <div className="jx-skeletonBlock jx-sharePreviewSkLine jx-sharePreviewSkLineShort" />
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    );
  }

  if (expired || !payload) {
    return (
      <div className="jx-sharePage">
        <div className="jx-shareCard jx-shareStateCard">
          <div className="jx-shareStateTitle">{t('链接已失效')}</div>
          <div className="jx-shareStateDesc">{t('该分享链接已失效，或内容已不可用。')}</div>
        </div>
      </div>
    );
  }

  return (
    <div className="jx-sharePage">
      <div className="jx-shareCard">
        <div className="jx-shareHeader">
          <div className="jx-shareHeaderTop">
            <div className="jx-shareEyebrow">{brandName}{t('会话分享页')}</div>
            <button className="jx-sharePrintBtn" onClick={() => window.print()}>
              {t('打印')}
            </button>
          </div>
          <h1 className="jx-shareTitle">{visibleShareTitle}</h1>
          <div className="jx-shareMeta">
            <span>{t('链接生成：{date}', { date: formatDateTime(payload.created_at, '') })}</span>
            <span>{t('有效期至：{date}', { date: formatShareExpiry(payload.expires_at) })}</span>
            <span className="jx-shareMetaId">{t('分享 ID：{id}', { id: payload.share_id })}</span>
          </div>
        </div>

        <div className="jx-shareMessages">
          {(payload.items || []).map((item, index) => (
            <div
              key={`${item.created_at || index}-${item.role}-${index}`}
              className={`jx-shareMessage ${item.role}`}
              /* Message-waterfall stagger index, capped at 8 to prevent tailing in long conversations (see share-preview.css) */
              style={staggerStyle(index)}
            >
              <div className="jx-shareMessageHeader">
                <div className="jx-shareMessageLabel">
                  {item.role === 'user'
                    ? t('用户: {name}', { name: payload.created_by_username || t('用户') })
                    : brandName}
                </div>
                {item.created_at && (
                  <div className="jx-shareMessageTime">{formatDateTime(item.created_at, '')}</div>
                )}
              </div>
              <div className={`jx-shareMessageBody jx-md${item.role === 'user' ? ' user' : ''}`}>
                {item.plan_data && (
                  <PlanCard
                    {...item.plan_data}
                    isStreaming={false}
                  />
                )}
                {item.content && (
                  <CitationMarkdownBlock
                    text={item.content}
                    isMarkdown={Boolean(item.is_markdown)}
                    citations={[]}
                  />
                )}
              </div>
            </div>
          ))}
        </div>

        <div className="jx-shareFootnote">{t('内容由AI生成，请注意甄别')}</div>
      </div>
    </div>
  );
}
