import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Alert, Button, Input, Modal, Tabs, TreeSelect, message } from 'antd';
import { FileOutlined, FolderOutlined, PictureOutlined, SearchOutlined, TeamOutlined, UserOutlined } from '@ant-design/icons';
import { useComposerDraft } from '../../stores/composerStore';
import { useDelayedFlag } from '../../hooks';
import { t } from '../../i18n';
import type { ResourceItem } from '../../types';
import { FilePreviewPane } from './FilePreviewPane';
import { FileList, FileListSkeleton, IMAGE_NAME } from './SpaceImportFileList';
import {
  listSpaceRoots, listSpaceEntries, referenceSpaceFile, spaceQuery,
  type SpaceEntry, type SpaceRoot,
} from '../myspace/spaceFilesApi';

interface Props {
  draftKey?: string;
  open: boolean;
  onClose: () => void;
  title?: string;
}

interface ScopeTreeNode {
  value: string;
  title: string;
  icon?: ReactNode;
  isLeaf?: boolean;
  children?: ScopeTreeNode[];
}

type Tab = 'all' | 'document' | 'image';
/** 目录树节点 key：空间 id 与目录路径拼在一起，空路径即空间根。 */
const scopeKey = (space: string, path: string) => `${space}|${path}`;
function parseScopeKey(key: string) {
  const split = key.indexOf('|');
  return { space: key.slice(0, split), path: key.slice(split + 1) };
}
/** 目录树只需要前几页的子目录，避免超大目录一次翻几十页。 */
const TREE_PAGE_LIMIT = 5;

function toItem(space: string, entry: SpaceEntry): ResourceItem {
  return {
    id: scopeKey(space, entry.path), file_id: space + ':' + entry.path, name: entry.name,
    type: IMAGE_NAME.test(entry.name) ? 'image' : 'document', size: entry.size,
    created_at: entry.modified_at || '', origin: 'cloud',
    download_url: '/v1/myspace/content?' + spaceQuery(space, entry.path),
  };
}

export function MySpaceImportModal({ draftKey, open, onClose, title }: Props) {
  const [roots, setRoots] = useState<SpaceRoot[]>([]);
  /** 已加载的子目录，key 为父节点 scopeKey。 */
  const [dirs, setDirs] = useState<Record<string, SpaceEntry[]>>({});
  const [scope, setScope] = useState({ space: '', path: '' });
  const [keyword, setKeyword] = useState('');
  /** 搜索交给后端，输入停顿后再发请求，避免每敲一个字打一次接口。 */
  const [debouncedKeyword, setDebouncedKeyword] = useState('');
  const [activeTab, setActiveTab] = useState<Tab>('all');
  const [items, setItems] = useState<ResourceItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [selected, setSelected] = useState<Map<string, ResourceItem>>(new Map());
  const [previewItem, setPreviewItem] = useState<ResourceItem | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const generation = useRef(0);
  const { addImportedSpaceFiles } = useComposerDraft(draftKey);

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedKeyword(keyword.trim()), 300);
    return () => clearTimeout(timer);
  }, [keyword]);

  const loadDirs = useCallback(async (space: string, path: string) => {
    const found: SpaceEntry[] = [];
    let next: string | undefined;
    for (let page = 0; page < TREE_PAGE_LIMIT; page++) {
      const result = await listSpaceEntries(space, path, next);
      found.push(...result.entries.filter(entry => entry.is_directory));
      next = result.next_cursor || undefined;
      if (!next) break;
    }
    setDirs(previous => ({ ...previous, [scopeKey(space, path)]: found }));
  }, []);

  // 打开时重置状态，加载空间列表和各空间的一级目录
  useEffect(() => {
    if (!open) return;
    const abort = new AbortController();
    setSelected(new Map()); setKeyword(''); setDebouncedKeyword('');
    setActiveTab('all'); setPreviewItem(null); setDirs({}); setError('');
    void listSpaceRoots(abort.signal).then(result => {
      setRoots(result.items);
      setScope({ space: result.items[0]?.space_id || '', path: '' });
      result.items.forEach(root => void loadDirs(root.space_id, '').catch(() => undefined));
    }).catch(reason => { if (!abort.signal.aborted) setError(String(reason)); });
    return () => abort.abort();
  }, [open, loadDirs]);

  const fetchPage = useCallback(async (next: string | undefined, token: number) => {
    if (next) setLoadingMore(true); else setLoading(true);
    setError('');
    try {
      const page = await listSpaceEntries(scope.space, scope.path, next, undefined, debouncedKeyword || undefined);
      if (generation.current !== token) return;
      const files = page.entries.filter(entry => !entry.is_directory).map(entry => toItem(scope.space, entry));
      setItems(previous => next ? [...previous, ...files] : files);
      setCursor(page.next_cursor);
    } catch (reason) {
      if (generation.current === token) setError(reason instanceof Error ? reason.message : t('加载失败'));
    } finally {
      if (generation.current === token) { setLoading(false); setLoadingMore(false); }
    }
  }, [scope, debouncedKeyword]);

  useEffect(() => {
    const token = ++generation.current;
    if (!open || !scope.space) return;
    setItems([]); setCursor(null);
    void fetchPage(undefined, token);
  }, [open, scope, debouncedKeyword, attempt, fetchPage]);

  const loadMore = () => {
    if (loading || loadingMore || !cursor || error) return;
    void fetchPage(cursor, generation.current);
  };

  const handleScopeChange = (key: string) => {
    setScope(parseScopeKey(key));
    setSelected(new Map()); setKeyword(''); setDebouncedKeyword(''); setPreviewItem(null);
  };

  const toggleItem = (id: string) => {
    setSelected(previous => {
      const next = new Map(previous);
      const item = items.find(candidate => candidate.id === id);
      if (next.has(id)) next.delete(id);
      else if (item) next.set(id, item);
      return next;
    });
  };

  const handleConfirm = async () => {
    setSubmitting(true);
    try {
      const files = [];
      for (const id of selected.keys()) {
        const { space, path } = parseScopeKey(id);
        files.push(await referenceSpaceFile(space, path));
      }
      if (files.length) addImportedSpaceFiles(files);
      onClose();
    } catch (reason) { void message.error(reason instanceof Error ? reason.message : String(reason)); }
    finally { setSubmitting(false); }
  };

  const scopeTreeData: ScopeTreeNode[] = useMemo(() => {
    const build = (space: string, path: string): ScopeTreeNode[] | undefined => {
      const children = dirs[scopeKey(space, path)];
      return children?.map(entry => {
        const sub = build(space, entry.path);
        return {
          value: scopeKey(space, entry.path), title: entry.name, icon: <FolderOutlined />,
          isLeaf: sub !== undefined && sub.length === 0, children: sub?.length ? sub : undefined,
        };
      });
    };
    return roots.map(root => ({
      value: scopeKey(root.space_id, ''), title: root.kind === 'personal' ? t('我的空间') : root.name,
      icon: root.kind === 'personal' ? <UserOutlined /> : <TeamOutlined />,
      children: build(root.space_id, '') || undefined,
    }));
  }, [roots, dirs]);

  const imageCount = items.filter(item => item.type === 'image').length;
  const counts = { all: items.length, document: items.length - imageCount, image: imageCount };
  const more = cursor ? '+' : '';
  const visibleItems = activeTab === 'all' ? items : items.filter(item => item.type === activeTab);
  const showSkeleton = useDelayedFlag(loading);
  const summaryText = selected.size > 0 ? t('已选 {n} 个文件', { n: selected.size }) : t('请选择要导入的文件');

  return (
    <Modal
      title={title || t('从我的空间导入')}
      open={open}
      onCancel={onClose}
      width={1080}
      footer={
        <div className="jx-spaceImportFooter">
          <span className="jx-spaceImportSummary">{summaryText}</span>
          <div className="jx-spaceImportFooterActions">
            <Button onClick={onClose} disabled={submitting}>{t('取消')}</Button>
            <Button type="primary" onClick={() => void handleConfirm()}
              disabled={selected.size === 0 || submitting} loading={submitting}>
              {t('确认导入')}
            </Button>
          </div>
        </div>
      }
      className="jx-spaceImportModal"
      destroyOnClose
    >
      <div className="jx-spaceImportLayout">
        <div className="jx-spaceImportLeft">
          <div className="jx-spaceImportControls">
            <TreeSelect
              /* 下拉面板比触发器宽得多，并限高滚动：文件夹名一长，200px 的面板会把每个
                 节点折成两三行，且整片浮层盖在右侧文件列表上（问题 43）。 */
              popupMatchSelectWidth={false}
              styles={{ popup: { root: { minWidth: 320, maxWidth: 460, maxHeight: 360, overflow: 'auto' } } }}
              treeTitleRender={(node) => (
                <span className="jx-spaceImportTreeNode" title={String(node.title ?? '')}>
                  {node.title}
                </span>
              )}
              value={scope.space ? scopeKey(scope.space, scope.path) : undefined}
              onChange={handleScopeChange}
              treeData={scopeTreeData}
              loadData={(node) => {
                const { space, path } = parseScopeKey(String(node.value));
                return loadDirs(space, path).catch(() => undefined);
              }}
              treeLine
              showSearch
              treeNodeFilterProp="title"
              placeholder={t('选择来源')}
            />
            <Input
              placeholder={t('搜索文件名')}
              prefix={<SearchOutlined />}
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              allowClear
            />
          </div>
          <Tabs
            activeKey={activeTab}
            onChange={(k) => setActiveTab(k as Tab)}
            size="small"
            items={[
              { key: 'all', label: `${t('全部')} (${counts.all}${more})` },
              {
                key: 'document',
                label: <span><FileOutlined style={{ marginRight: 4 }} />{t('文档')} ({counts.document}{more})</span>,
              },
              {
                key: 'image',
                label: <span><PictureOutlined style={{ marginRight: 4 }} />{t('图片')} ({counts.image}{more})</span>,
              },
            ]}
          />
          {error && !loading && (
            <Alert type="error" showIcon title={t('加载失败')} description={error}
              action={<Button size="small" onClick={() => setAttempt(value => value + 1)}>{t('重试')}</Button>} />
          )}
          {showSkeleton ? (
            <FileListSkeleton />
          ) : loading || (error && items.length === 0) ? null : (
            <FileList
              items={visibleItems}
              selected={new Set(selected.keys())}
              previewId={previewItem?.id ?? null}
              onToggle={toggleItem}
              onPreview={setPreviewItem}
              onScrollEnd={loadMore}
              footer={items.length > 0 ? (
                <div className="jx-spaceImportLoadMore">
                  {loadingMore ? (
                    <span className="jx-spaceImportLoadMore-hint">{t('加载中…')}</span>
                  ) : cursor ? (
                    <Button size="small" onClick={loadMore}>{t('加载更多')}</Button>
                  ) : (
                    <span className="jx-spaceImportLoadMore-hint">{t('已加载全部内容')}</span>
                  )}
                </div>
              ) : null}
            />
          )}
        </div>
        <div className="jx-spaceImportRight">
          <FilePreviewPane item={previewItem} />
        </div>
      </div>
    </Modal>
  );
}
