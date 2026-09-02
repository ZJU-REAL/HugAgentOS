import { useEffect, useMemo, useRef, useState } from 'react';
import { Input, Modal } from 'antd';
import type { InputRef } from 'antd';
import { CloseOutlined, SearchOutlined } from '@ant-design/icons';
import type { ChatShareRecord } from '../../api';
import { getArtifacts, getFavoriteChats, listChatShares } from '../../api';
import type { KBItem, ResourceItem } from '../../types';
import { useMySpaceStore } from '../../stores/mySpaceStore';
import { useCatalogStore } from '../../stores/catalogStore';
import { getFileIconSrc } from '../../utils/fileIcon';
import { highlightKeyword } from '../../utils/highlight';
import { t } from '../../i18n';

interface Props {
  onOpenChat: (chatId: string) => void;
  onPreviewFile: (item: ResourceItem) => void;
}

interface SearchHits {
  files: ResourceItem[];
  kbs: KBItem[];
  favorites: ResourceItem[];
  shares: ChatShareRecord[];
}

const EMPTY_HITS: SearchHits = { files: [], kbs: [], favorites: [], shares: [] };
const PER_GROUP = 20;
const DEBOUNCE_MS = 300;

function countHits(hits: SearchHits): number {
  return hits.files.length + hits.kbs.length + hits.favorites.length + hits.shares.length;
}

/**
 * 「我的空间」的搜索弹窗：形态与左侧边栏的全局搜索（⌘K）一致，由二级栏的「搜索」项唤起。
 * 搜索范围是云文档 / 知识库 / 会话收藏 / 分享记录——会话正文由那个全局搜索负责，这里不重复。
 * 文件与收藏走各自已有的带 keyword 的接口；知识库与分享记录的列表本就整份在手，本地过滤即可。
 */
export function MySpaceSearchModal({ onOpenChat, onPreviewFile }: Props) {
  const searchOpen = useMySpaceStore((s) => s.searchOpen);
  const closeSearch = useMySpaceStore((s) => s.closeSearch);
  const globalQuery = useMySpaceStore((s) => s.globalQuery);
  const setGlobalQuery = useMySpaceStore((s) => s.setGlobalQuery);
  const setTab = useMySpaceStore((s) => s.setTab);
  const setKbTab = useCatalogStore((s) => s.setKbTab);
  const catalog = useCatalogStore((s) => s.catalog);

  /** 结果带上它对应的查询词：与当前输入不一致就说明还在路上，据此显示骨架屏 */
  const [result, setResult] = useState<{ query: string; hits: SearchHits }>({ query: '', hits: EMPTY_HITS });
  /** 递增序号：只认最后一次请求的结果，避免慢的旧请求盖掉新结果 */
  const seqRef = useRef(0);
  const inputRef = useRef<InputRef>(null);

  const keyword = globalQuery.trim();

  useEffect(() => {
    if (!searchOpen || !keyword) return;
    const seq = ++seqRef.current;
    const timer = window.setTimeout(() => {
      void (async () => {
        const lower = keyword.toLowerCase();
        const [files, favorites, shares] = await Promise.all([
          getArtifacts({ keyword, scope: 'all', page_size: PER_GROUP }).then((r) => r.items).catch(() => []),
          getFavoriteChats({ keyword, page_size: PER_GROUP }).then((r) => r.items).catch(() => []),
          listChatShares().catch((): ChatShareRecord[] => []),
        ]);
        if (seq !== seqRef.current) return;
        setResult({
          query: keyword,
          hits: {
            files,
            favorites,
            kbs: (catalog.kb || []).filter((kb) => (
              kb.name.toLowerCase().includes(lower) || (kb.desc || '').toLowerCase().includes(lower)
            )).slice(0, PER_GROUP),
            shares: shares.filter((s) => (s.title || '').toLowerCase().includes(lower)).slice(0, PER_GROUP),
          },
        });
      })();
    }, DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [searchOpen, keyword, catalog.kb]);

  const hits = result.hits;
  const total = useMemo(() => countHits(hits), [hits]);
  const ready = result.query === keyword;

  const pick = (run: () => void) => {
    closeSearch();
    run();
  };

  let body: React.ReactNode;
  if (!keyword) {
    body = (
      <div key="hint" className="jx-searchEmptyState">
        <SearchOutlined className="jx-searchEmptyIcon" />
        <div className="jx-searchEmptyTitle">{t('搜索我的空间')}</div>
        <div className="jx-searchEmptyHint">{t('云文档、知识库、会话收藏与分享记录')}</div>
      </div>
    );
  } else if (!ready) {
    body = (
      <div key="skeleton" className="jx-searchSkeletonList" aria-hidden="true" aria-busy="true">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="jx-searchSkeletonItem">
            <div className="jx-skeletonBlock jx-searchSkTitle" />
            <div className="jx-skeletonBlock jx-searchSkSnippet" />
          </div>
        ))}
      </div>
    );
  } else if (total === 0) {
    body = (
      <div key="empty" className="jx-searchEmptyState">
        <SearchOutlined className="jx-searchEmptyIcon" />
        <div className="jx-searchEmptyTitle">{t('无匹配结果')}</div>
        <div className="jx-searchEmptyHint">{t('试试换个关键词')}</div>
      </div>
    );
  } else {
    body = (
      <div key="results" className="jx-searchGroupList">
        <Group title={t('云文档')} count={hits.files.length}>
          {hits.files.map((item) => (
            <Row
              key={`file-${item.id}`}
              keyword={keyword}
              icon={<img src={getFileIconSrc(item.name)} alt="" className="jx-msSearchIcon" />}
              title={item.name}
              sub={item.source_chat_title || item.content_preview}
              onClick={() => pick(() => onPreviewFile(item))}
            />
          ))}
        </Group>

        <Group title={t('知识库')} count={hits.kbs.length}>
          {hits.kbs.map((kb) => (
            <Row
              key={`kb-${kb.id}`}
              keyword={keyword}
              title={kb.name}
              sub={kb.desc}
              onClick={() => pick(() => {
                setKbTab(kb.visibility === 'private' || (!kb.visibility && !kb.is_public) ? 'private' : 'public');
                setTab('kb');
              })}
            />
          ))}
        </Group>

        <Group title={t('会话收藏')} count={hits.favorites.length}>
          {hits.favorites.map((item) => (
            <Row
              key={`fav-${item.id}`}
              keyword={keyword}
              title={item.name}
              sub={item.content_preview}
              onClick={() => pick(() => {
                if (item.source_chat_id) onOpenChat(item.source_chat_id);
              })}
            />
          ))}
        </Group>

        <Group title={t('分享记录')} count={hits.shares.length}>
          {hits.shares.map((s) => (
            <Row
              key={`share-${s.share_id}`}
              keyword={keyword}
              title={s.title}
              sub={s.status === 'expired' ? t('已过期') : t('有效')}
              onClick={() => pick(() => window.open(s.preview_url, '_blank', 'noopener'))}
            />
          ))}
        </Group>
      </div>
    );
  }

  return (
    <Modal
      open={searchOpen}
      onCancel={closeSearch}
      footer={null}
      closable={false}
      width={640}
      maskClosable
      destroyOnHidden
      className="jx-searchModal"
      afterOpenChange={(open) => { if (open) inputRef.current?.focus(); }}
      aria-label={t('搜索我的空间')}
      style={{ top: 96 }}
    >
      <div className="jx-searchModalHeader">
        <SearchOutlined className="jx-searchModalIcon" />
        <Input
          ref={inputRef}
          variant="borderless"
          placeholder={t('搜索云文档、知识库、会话收藏与分享记录…')}
          value={globalQuery}
          onChange={(e) => setGlobalQuery(e.target.value)}
          className="jx-searchModalInput"
        />
        <button
          type="button"
          className="jx-searchModalClose"
          onClick={closeSearch}
          aria-label={t('关闭')}
        >
          <CloseOutlined />
        </button>
      </div>

      <div className="jx-searchModalBody">{body}</div>
    </Modal>
  );
}

function Group({ title, count, children }: {
  title: string;
  count: number;
  children: React.ReactNode;
}) {
  if (count === 0) return null;
  return (
    <div className="jx-searchGroup">
      <div className="jx-searchGroupTitle">{title}</div>
      <div className="jx-searchGroupItems">{children}</div>
    </div>
  );
}

function Row({ title, sub, keyword, icon, onClick }: {
  title: string;
  sub?: string | null;
  keyword: string;
  icon?: React.ReactNode;
  onClick: () => void;
}) {
  return (
    <div className="jx-searchItem" onClick={onClick}>
      {icon ?? <span className="jx-searchItemTypeIcon jx-searchItemTypeIcon--dot" aria-hidden="true" />}
      <div className="jx-searchItemMain">
        <span className="jx-searchItemTitle">{highlightKeyword(title, keyword)}</span>
        {sub && <span className="jx-searchItemSnippet">{sub}</span>}
      </div>
    </div>
  );
}
