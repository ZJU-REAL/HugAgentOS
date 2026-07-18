import { useEffect, useMemo, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { Button, Input, Popconfirm, Popover, Tag, Tooltip, Typography, message } from 'antd';
import { t } from '../../i18n';
import { ExportOutlined, EyeOutlined, InfoCircleOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import { CopyButton } from '../common/CopyButton';
import { EASE } from '../../utils/motionTokens';
import { deleteChatShare, listChatShares, listSites, restoreChatShare, revokeChatShare, type ChatShareRecord, type SiteItem } from '../../api';
import { useAuthStore, useCatalogStore, useChatStore } from '../../stores';
import { userScopedKey } from '../../storage';
import { formatDateTime } from '../../utils/date';
import '../../styles/sites.css';

const SHARE_RECORDS_CACHE_KEY = 'hugagent_share_records_cache';

/** Published sites are also a kind of outbound share link — show a compact list at the top of the share records page. */
function SiteShareSection() {
  const [sites, setSites] = useState<SiteItem[]>([]);

  useEffect(() => {
    void listSites().then((r) => setSites(r.items)).catch(() => setSites([]));
  }, []);

  if (!sites.length) return null;

  return (
    <div className="jx-shareSites">
      <div className="jx-shareSitesTitle">{t('已发布站点')}（{sites.length}）</div>
      {sites.map((site) => {
        const fullUrl = `${window.location.origin}${site.url}`;
        return (
          <div key={site.site_id} className="jx-shareSiteRow">
            <span className="jx-shareSiteName">{site.title}</span>
            <a className="jx-shareSiteUrl" href={site.url} target="_blank" rel="noopener noreferrer">{fullUrl}</a>
            <Tag color={site.visibility === 'public' ? 'blue' : site.visibility === 'team' ? 'geekblue' : undefined}>
              {site.visibility === 'public' ? t('公开') : site.visibility === 'team' ? t('团队') : t('私密')}
            </Tag>
            <span className="jx-shareSiteViews"><EyeOutlined /> {site.view_count}</span>
            <CopyButton text={fullUrl} size="small" />
          </div>
        );
      })}
      <div className="jx-shareSitesHint">{t('站点的可见性与删除在「实验室 → 站点」中管理')}</div>
    </div>
  );
}

function formatShareExpiry(value?: string | null) {
  if (!value) return t('长期');
  return formatDateTime(value, '--');
}

function getShareStatusLabel(record: ChatShareRecord) {
  return record.status === 'valid' ? t('生效中') : t('已失效');
}

function getShareExpiryLabel(record: ChatShareRecord) {
  if (record.expiry_option === '3d') return t('有效期3天');
  if (record.expiry_option === '15d') return t('有效期15天');
  if (record.expiry_option === '3m') return t('有效期3个月');
  if (record.expiry_option === 'permanent' || !record.expires_at) return t('长期有效');

  const createdAt = new Date(record.created_at).getTime();
  const expiresAt = new Date(record.expires_at).getTime();
  if (!Number.isNaN(createdAt) && !Number.isNaN(expiresAt)) {
    const diffDays = (expiresAt - createdAt) / (24 * 60 * 60 * 1000);
    if (diffDays <= 3.5) return t('有效期3天');
    if (diffDays <= 15.5) return t('有效期15天');
    if (diffDays <= 95) return t('有效期3个月');
  }

  return t('有效期');
}

function isShareWithinExpiry(expiresAt?: string | null) {
  if (!expiresAt) return true;
  const ts = new Date(expiresAt).getTime();
  return !Number.isNaN(ts) && ts > Date.now();
}

function loadCachedShareRecords(userId: string | null | undefined): ChatShareRecord[] {
  if (typeof window === 'undefined') return [];
  const key = userScopedKey(SHARE_RECORDS_CACHE_KEY, userId);
  if (!key) return [];
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function saveCachedShareRecords(userId: string | null | undefined, records: ChatShareRecord[]) {
  if (typeof window === 'undefined') return;
  const key = userScopedKey(SHARE_RECORDS_CACHE_KEY, userId);
  if (!key) return;
  try {
    window.localStorage.setItem(key, JSON.stringify(records));
  } catch {
    // ignore cache write errors
  }
}

function copyTextFallback(text: string) {
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', 'true');
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  textarea.style.pointerEvents = 'none';
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  textarea.setSelectionRange(0, textarea.value.length);
  const copied = document.execCommand('copy');
  document.body.removeChild(textarea);
  return copied;
}

interface ShareRecordsPageProps {
  embedded?: boolean;
  hideEmbeddedDesc?: boolean;
}

export default function ShareRecordsPage({ embedded = false, hideEmbeddedDesc = false }: ShareRecordsPageProps) {
  const authUserId = useAuthStore((s) => s.authUser?.user_id ?? null);
  const [records, setRecords] = useState<ChatShareRecord[]>(() => loadCachedShareRecords(authUserId));
  const [loading, setLoading] = useState(true);
  const [keyword, setKeyword] = useState('');
  const [messageApi, contextHolder] = message.useMessage();
  const loadingCardCount = useMemo(() => {
    if (records.length > 0) return Math.min(Math.max(records.length, 1), 4);
    return embedded ? 3 : 4;
  }, [embedded, records.length]);
  const loadingCards = useMemo(
    () => Array.from({ length: loadingCardCount }, (_, index) => index),
    [loadingCardCount],
  );

  const handleJumpToOriginChat = (record: ChatShareRecord) => {
    useCatalogStore.getState().setPanel('chat');
    useChatStore.getState().setCurrentChatId(record.chat_id);
    useChatStore.getState().setPendingScrollMessageTs(record.origin_message_ts ?? null);
  };

  const loadRecords = async (options?: { silent?: boolean }) => {
    const silent = options?.silent ?? false;
    if (!silent) setLoading(true);
    try {
      const items = await listChatShares();
      setRecords(items);
      saveCachedShareRecords(authUserId, items);
    } catch (error) {
      messageApi.error(error instanceof Error ? error.message : t('加载分享记录失败'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadRecords();
  }, []);

  const sortedRecords = useMemo(
    () => [...records].sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()),
    [records],
  );

  const filteredRecords = useMemo(() => {
    const query = keyword.trim().toLowerCase();
    if (!query) return sortedRecords;
    return sortedRecords.filter((record) => (
      `${record.title}`.toLowerCase().includes(query)
      || `${record.share_id}`.toLowerCase().includes(query)
    ));
  }, [keyword, sortedRecords]);

  const handleCopyLink = async (previewUrl: string): Promise<boolean> => {
    const targetUrl = new URL(previewUrl, window.location.origin).toString();
    try {
      if (navigator.clipboard) {
        await navigator.clipboard.writeText(targetUrl);
      } else {
        const copied = copyTextFallback(targetUrl);
        if (!copied) throw new Error('copy_failed');
      }
      messageApi.success(t('分享链接已复制'));
      return true;
    } catch {
      const copied = copyTextFallback(targetUrl);
      if (copied) {
        messageApi.success(t('分享链接已复制'));
        return true;
      }
      messageApi.error(t('复制失败，请手动复制'));
      return false;
    }
  };

  const handleRevoke = async (shareId: string) => {
    try {
      await revokeChatShare(shareId);
      setRecords((current) => {
        const next = current.map((record) => (
          record.share_id === shareId
            ? { ...record, status: 'expired' as const, revoked: true }
            : record
        ));
        saveCachedShareRecords(authUserId, next);
        return next;
      });
      messageApi.success(t('访问已终止'));
    } catch (error) {
      messageApi.error(error instanceof Error ? error.message : t('终止访问失败'));
    }
  };

  const handleDelete = async (shareId: string) => {
    try {
      await deleteChatShare(shareId);
      setRecords((current) => {
        const next = current.filter((record) => record.share_id !== shareId);
        saveCachedShareRecords(authUserId, next);
        return next;
      });
      messageApi.success(t('分享记录已删除'));
    } catch (error) {
      messageApi.error(error instanceof Error ? error.message : t('删除失败'));
    }
  };

  const handleRestore = async (shareId: string) => {
    try {
      await restoreChatShare(shareId);
      setRecords((current) => {
        const next = current.map((record) => (
          record.share_id === shareId
            ? { ...record, status: 'valid' as const, revoked: false }
            : record
        ));
        saveCachedShareRecords(authUserId, next);
        return next;
      });
      messageApi.success(t('访问已启用'));
    } catch (error) {
      messageApi.error(error instanceof Error ? error.message : t('启用访问失败'));
    }
  };

  return (
    <div className={`jx-shareRecordsPage${embedded ? ' embedded' : ''}`}>
      {contextHolder}
      <div className={`jx-shareRecordsHead${embedded && hideEmbeddedDesc ? ' noDesc' : ''}`}>
        <div className="jx-shareRecordsHeadMain">
          {!embedded && <h2 className="jx-shareRecordsTitle">{t('分享记录')}</h2>}
          {!(embedded && hideEmbeddedDesc) && (
            <p className="jx-shareRecordsDesc">{t('查看并管理已生成的分享链接与有效状态，查看浏览量')}</p>
          )}
        </div>
        <div className="jx-shareRecordsToolbar">
          <Input
            allowClear
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder={t('搜索标题关键词/分享ID')}
            prefix={<SearchOutlined />}
            className="jx-shareRecordsSearch"
          />
          <Button icon={<ReloadOutlined />} onClick={() => void loadRecords()} disabled={loading}>
            {t('刷新')}
          </Button>
        </div>
      </div>

      <SiteShareSection />

      {loading ? (
        <div className="jx-shareRecordsLoading">
          {loadingCards.map((item) => (
            <div key={item} className="jx-shareRecordCard jx-shareRecordCardSkeleton" aria-hidden="true">
              <div className="jx-shareRecordTop">
                <div className="jx-shareRecordMain">
                  <div className="jx-shareRecordTitleRow">
                    <div className="jx-skeletonBlock jx-shareSkTitle" />
                    <div className="jx-skeletonBlock jx-shareSkTag" />
                    <div className="jx-skeletonBlock jx-shareSkTag jx-shareSkTagWide" />
                  </div>
                  <div className="jx-shareRecordMeta">
                    <div className="jx-skeletonBlock jx-shareSkMeta" />
                    <div className="jx-skeletonBlock jx-shareSkMeta" />
                    <div className="jx-skeletonBlock jx-shareSkMeta jx-shareSkMetaShort" />
                  </div>
                </div>
                <div className="jx-shareRecordSide jx-shareRecordSideSkeleton">
                  <div className="jx-skeletonBlock jx-shareSkAction" />
                  <div className="jx-skeletonBlock jx-shareSkAction" />
                  <div className="jx-shareRecordViewsRow">
                    <div className="jx-skeletonBlock jx-shareSkViews" />
                    <div className="jx-skeletonBlock jx-shareSkTextBtn" />
                  </div>
                </div>
              </div>
            </div>
          ))}
        </div>
      ) : records.length === 0 ? (
        <div className="jx-shareRecordsEmpty">
          <Typography.Text type="secondary">{t('暂无分享记录')}</Typography.Text>
        </div>
      ) : filteredRecords.length === 0 ? (
        <div className="jx-shareRecordsEmpty">
          <Typography.Text type="secondary">{t('没有匹配的分享记录')}</Typography.Text>
        </div>
      ) : (
        <div className="jx-shareRecordsList jx-anim-fadeIn">
          {/* initial=false: the whole list does not replay on first paint / refresh; only deletion/filtering triggers the exit collapse; layout lets the cards below smoothly fill in */}
          <AnimatePresence initial={false}>
          {filteredRecords.map((record) => (
            <motion.div
              key={record.share_id}
              layout
              className="jx-shareRecordCard"
              exit={{ opacity: 0, height: 0, paddingTop: 0, paddingBottom: 0 }}
              transition={{ duration: 0.25, ease: EASE.standard }}
              style={{ overflow: 'hidden' }}
            >
              <div className="jx-shareRecordTop">
                <div className="jx-shareRecordMain">
                  <div className="jx-shareRecordTitleRow">
                    <button
                      type="button"
                      className="jx-shareRecordTitleBtn"
                      onClick={() => window.open(record.preview_url, '_blank', 'noopener,noreferrer')}
                    >
                      <span className="jx-shareRecordTitle">{record.title || t('未命名分享')}</span>
                    </button>
                    {/* Status flip (terminate/enable) keyed pop-in confirmation */}
                    <motion.span
                      key={`${record.share_id}-${record.status}`}
                      style={{ display: 'inline-block' }}
                      initial={{ scale: 0.8, opacity: 0 }}
                      animate={{ scale: 1, opacity: 1 }}
                      transition={{ duration: 0.18, ease: 'backOut' }}
                    >
                      <Tag color={record.status === 'valid' ? 'green' : 'default'}>
                        {getShareStatusLabel(record)}
                      </Tag>
                    </motion.span>
                    <Tag style={{ background: '#EBF2FF', borderColor: '#DBE9FF', color: '#126DFF' }}>
                      {getShareExpiryLabel(record)}
                    </Tag>
                    <button
                      type="button"
                      className="jx-shareRecordJumpBtn"
                      onClick={() => handleJumpToOriginChat(record)}
                      title={t('跳转关联会话记录')}
                    >
                      <img src="/home/share-link-gray.svg" alt={t('跳转关联会话记录')} className="jx-shareRecordJumpIcon" />
                    </button>
                  </div>
                  <div className="jx-shareRecordMeta">
                    <span>{t('链接生成：{date}', { date: formatDateTime(record.created_at, '--') })}</span>
                    <span>{t('有效期至：{date}', { date: formatShareExpiry(record.expires_at) })}</span>
                    <Popover
                      trigger="click"
                      placement="bottomLeft"
                      overlayClassName="jx-shareRecordIdPopover"
                      content={<span className="jx-shareRecordIdValue">{record.share_id}</span>}
                    >
                      <button type="button" className="jx-shareRecordMetaBtn">
                        <span>{t('分享ID')}</span>
                        <InfoCircleOutlined />
                      </button>
                    </Popover>
                  </div>
                </div>
                <div className="jx-shareRecordSide">
                  <CopyButton
                    className="jx-shareRecordActionCopy"
                    onCopy={() => handleCopyLink(record.preview_url)}
                  >
                    {t('复制链接')}
                  </CopyButton>
                  <Button
                    icon={<ExportOutlined />}
                    onClick={() => window.open(record.preview_url, '_blank', 'noopener,noreferrer')}
                    className="jx-shareRecordActionPreview"
                  >
                    {t('打开预览')}
                  </Button>
                  <div className="jx-shareRecordViewsRow">
                    <Tooltip title={t('总浏览量')}>
                      <span className="jx-shareRecordViewsMain" aria-label={t('浏览 {n} 次', { n: record.view_count ?? 0 })}>
                        <EyeOutlined />
                        <span>{record.view_count ?? 0}</span>
                      </span>
                    </Tooltip>
                    <span className="jx-shareRecordActions">
                      {record.status === 'valid' ? (
                        <>
                          <Popconfirm
                            title={t('确认终止该访问？')}
                            description={t('终止后，该链接将立即失效，无法继续访问')}
                            okText={t('确认')}
                            cancelText={t('取消')}
                            onConfirm={() => void handleRevoke(record.share_id)}
                          >
                            <button type="button" className="jx-shareRecordTerminateBtn">
                              {t('终止访问')}
                            </button>
                          </Popconfirm>
                          <span className="jx-shareRecordActionDivider" aria-hidden="true" />
                        </>
                      ) : record.revoked && isShareWithinExpiry(record.expires_at) ? (
                        <>
                          <button type="button" className="jx-shareRecordTerminateBtn" onClick={() => void handleRestore(record.share_id)}>
                            {t('启用访问')}
                          </button>
                          <span className="jx-shareRecordActionDivider" aria-hidden="true" />
                        </>
                      ) : null}
                      <Popconfirm
                        title={t('确认删除该分享记录？')}
                        description={t('删除后链接立即失效且无法恢复')}
                        okText={t('确认删除')}
                        cancelText={t('取消')}
                        okButtonProps={{ danger: true }}
                        onConfirm={() => void handleDelete(record.share_id)}
                      >
                        <button type="button" className="jx-shareRecordTerminateBtn jx-shareRecordDeleteBtn">
                          {t('删除')}
                        </button>
                      </Popconfirm>
                    </span>
                  </div>
                </div>
              </div>
            </motion.div>
          ))}
          </AnimatePresence>
        </div>
      )}
    </div>
  );
}
