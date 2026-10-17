import { EditOutlined, FileMarkdownOutlined, FileOutlined, FolderOpenOutlined, FolderOutlined, LinkOutlined, ReloadOutlined } from '@ant-design/icons';
import { Button, Empty, Input, Spin, Tooltip, Tree, message } from 'antd';
import type { TreeDataNode } from 'antd';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { t } from '../../../i18n';
import { userMarkdownHtml } from '../../../utils/userMarkdown';
import { useAgentNameStore } from '../shared/agentName';
import { errorText, personalApi } from '../api';
import type { WorkspaceEntry, WorkspaceFile, WorkspaceTree } from '../api';
import './agent-workspace.css';

const ROOT = '/workspace';
const DEFAULT_FILE = ROOT + '/SOUL.md';

/** Role descriptions come from the backend in Chinese; they are dictionary keys too. */
function roleLabel(role?: string | null): string {
  return role ? t(role) : '';
}

function formatSize(size: number | null): string {
  if (size == null) return '';
  if (size < 1024) return size + ' B';
  if (size < 1024 * 1024) return (size / 1024).toFixed(1) + ' KB';
  return (size / 1024 / 1024).toFixed(1) + ' MB';
}

function entryIcon(entry: WorkspaceEntry, open: boolean) {
  if (entry.type === 'dir') return open ? <FolderOpenOutlined /> : <FolderOutlined />;
  if (entry.type === 'link') return <LinkOutlined />;
  return entry.name.endsWith('../.md') ? <FileMarkdownOutlined /> : <FileOutlined />;
}

function buildTree(entries: WorkspaceEntry[], expanded: Set<string>): TreeDataNode[] {
  const nodes = new Map<string, TreeDataNode & { children?: TreeDataNode[] }>();
  const roots: TreeDataNode[] = [];
  for (const entry of entries) {
    const role = roleLabel(entry.role);
    const node: TreeDataNode & { children?: TreeDataNode[] } = {
      key: entry.path,
      isLeaf: entry.type !== 'dir',
      selectable: entry.type !== 'link' && entry.type !== 'other',
      title: (
        <Tooltip title={role || undefined} placement="right" mouseEnterDelay={0.4}>
          <span className={`pa-ws-node pa-ws-node--${entry.type}`}>
            <span className="pa-ws-nodeIcon">{entryIcon(entry, expanded.has(entry.path))}</span>
            <span className="pa-ws-nodeName">{entry.name}</span>
            {role && <span className="pa-ws-nodeRole">{role}</span>}
          </span>
        </Tooltip>
      ),
      children: entry.type === 'dir' ? [] : undefined,
    };
    nodes.set(entry.path, node);
    const parent = nodes.get(entry.path.slice(0, entry.path.lastIndexOf('/')));
    if (parent?.children) parent.children.push(node);
    else roots.push(node);
  }
  return roots;
}

/** My Space view of the Personal Agent's permanent workspace files. */
export function AgentWorkspaceView() {
  const [tree, setTree] = useState<WorkspaceTree | null>(null);
  const [selected, setSelected] = useState<string>(DEFAULT_FILE);
  const [file, setFile] = useState<WorkspaceFile | null>(null);
  const [fileError, setFileError] = useState('');
  const [draft, setDraft] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [expanded, setExpanded] = useState<string[] | null>(null);

  const loadTree = useCallback(async () => {
    try { setTree(await personalApi.workspaceTree()); }
    catch (exc) { message.error(errorText(exc)); }
  }, []);

  const loadFile = useCallback(async (path: string) => {
    setFile(null); setFileError(''); setDraft(null);
    try { setFile(await personalApi.workspaceFile(path)); }
    catch (exc) { setFileError(errorText(exc)); }
  }, []);

  useEffect(() => { void loadTree(); }, [loadTree]);
  useEffect(() => { if (tree?.exists) void loadFile(selected); }, [tree?.exists, selected, loadFile]);

  // Top-level directories start open; afterwards the user's choice is kept across refreshes.
  const expandedKeys = useMemo(() => expanded ?? (tree?.entries ?? []).filter((e) => e.type === 'dir' && e.path.split('/').length <= 3).map((e) => e.path), [expanded, tree]);
  const treeData = useMemo(() => buildTree(tree?.entries ?? [], new Set(expandedKeys)), [tree, expandedKeys]);
  const entryTypes = useMemo(() => new Map((tree?.entries ?? []).map((e) => [e.path, e.type])), [tree]);

  const toggleDir = (path: string) => setExpanded(expandedKeys.includes(path) ? expandedKeys.filter((k) => k !== path) : [...expandedKeys, path]);

  const openFile = (path: string) => {
    if (path === selected) return;
    if (draft !== null && draft !== file?.content && !window.confirm(t('放弃未保存的修改？'))) return;
    setSelected(path);
  };

  const save = async () => {
    if (!file || draft === null) return;
    setSaving(true);
    try {
      setFile(await personalApi.saveWorkspaceFile(file.path, draft, file.etag));
      setDraft(null);
      message.success(t('已保存，下一个任务开始生效'));
      void loadTree();
      // The name line of IDENTITY.md renames the agent; refresh the navigation labels.
      if (file.path === ROOT + '/IDENTITY.md') void useAgentNameStore.getState().reload();
    } catch (exc) {
      message.error(errorText(exc) === 'File changed since it was opened' ? t('文件已被智能体修改，请刷新后重试') : errorText(exc));
    } finally { setSaving(false); }
  };

  const reload = () => { void loadTree(); void loadFile(selected); };

  if (tree && !tree.available) {
    return <Empty className="pa-ws-empty" description={t('当前部署未配置个人智能体工作区，请联系管理员设置 PERSONAL_AGENT_VOLUME_ROOT。')} />;
  }

  return (
    <div className="pa-ws">
      <div className="pa-ws-toolbar">
        <span className="pa-ws-hint">{t('智能体的人格、用户画像和记忆以 Markdown 文件存放在 /workspace，每个任务开始时注入提示词。')}</span>
        <Button size="small" icon={<ReloadOutlined />} onClick={reload}>{t('刷新')}</Button>
      </div>

      <div className="pa-ws-split">
        <aside className="pa-ws-tree">
          <div className="pa-ws-treeHead">/workspace</div>
          <div className="pa-ws-treeBody">
            {!tree ? <Spin /> : !tree.exists ? (
              <Empty description={t('工作区尚未创建')} image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ) : (
              <>
                <Tree
                  blockNode
                  treeData={treeData}
                  selectedKeys={[selected]}
                  expandedKeys={expandedKeys}
                  onExpand={(keys) => setExpanded(keys.map(String))}
                  onSelect={(_, { node }) => {
                    const path = String(node.key);
                    if (entryTypes.get(path) === 'dir') toggleDir(path);
                    else openFile(path);
                  }}
                />
                {tree.truncated && <div className="pa-ws-note">{t('文件过多，仅显示部分')}</div>}
              </>
            )}
          </div>
        </aside>
        <section className="pa-ws-viewer">
          <div className="pa-ws-viewerHead">
            <div className="pa-ws-path">
              <strong>{selected.replace(ROOT + '/', '')}</strong>
              {file?.role && <span>{roleLabel(file.role)}</span>}
            </div>
            {file && <span className="pa-ws-meta">{formatSize(file.size)} · {new Date(file.modified * 1000).toLocaleString()}</span>}
            {file?.editable && (draft === null ? (
              <Button size="small" icon={<EditOutlined />} onClick={() => setDraft(file.content ?? '')}>{t('编辑')}</Button>
            ) : (
              <span className="pa-ws-actions">
                <Button size="small" onClick={() => setDraft(null)}>{t('取消')}</Button>
                <Button size="small" type="primary" loading={saving} onClick={() => void save()}>{t('保存')}</Button>
              </span>
            ))}
          </div>
          <div className="pa-ws-viewerBody">
            {fileError ? <Empty description={fileError} image={Empty.PRESENTED_IMAGE_SIMPLE} />
              : !file ? <Spin />
              : draft !== null ? (
                <Input.TextArea className="pa-ws-editor" value={draft} onChange={(e) => setDraft(e.target.value)} spellCheck={false} />
              ) : file.binary ? (
                <Empty description={t('二进制文件不支持预览')} image={Empty.PRESENTED_IMAGE_SIMPLE} />
              ) : file.path.endsWith('../.md') ? (
                <div className="jx-md pa-ws-markdown" dangerouslySetInnerHTML={{ __html: userMarkdownHtml(file.content ?? '') }} />
              ) : (
                <pre className="pa-ws-text">{file.content}</pre>
              )}
            {file?.truncated && <div className="pa-ws-note">{t('文件较大，仅显示前 1 MB')}</div>}
          </div>
        </section>
      </div>
    </div>
  );
}
