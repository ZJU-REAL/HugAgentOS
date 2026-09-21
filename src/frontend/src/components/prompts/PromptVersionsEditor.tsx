import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Button, Drawer, Form, Input, Modal, Popconfirm, Select, Space,
  Switch, Table, Tabs, Tag, Tooltip, Upload, message,
} from 'antd';
import {
  CheckCircleTwoTone, CopyOutlined, DeleteOutlined, EditOutlined,
  PlusOutlined, ReloadOutlined, RollbackOutlined,
  ArrowDownOutlined, ArrowUpOutlined, ThunderboltOutlined,
  ExportOutlined, ImportOutlined,
} from '@ant-design/icons';
import { promptFetch as adminFetch } from './promptApi';
import { formatDateTime, formatDateKey } from '../../utils/date';
import { useFlashKey } from '../../hooks/useFlash';
import { useResizableColumns } from '../common/ResizableColumns';
import { t } from '../../i18n';
import { PromptPreviewModal, type PromptPreview } from './PromptPreviewModal';

// kind 不再是写死的枚举：管理员可以在这里新开 tab（后端 custom_kinds），
// 「模式选择」页随后就能绑定它。内置那批仍有固定标签与文件系统兜底。
type Kind = string;

interface KindMeta { key: Kind; label: string; builtin: boolean; visible?: boolean; preview?: string; description?: string }

interface VersionSummary {
  id: string;
  kind: Kind;
  name: string;
  description: string;
  parts_count: number;
  is_active: boolean;
  created_at: string | null;
  updated_at: string | null;
}

interface PartDetail {
  part_id: string;
  display_name: string;
  content: string;
  sort_order: number;
  is_enabled: boolean;
}

interface VersionDetail extends VersionSummary {
  parts: PartDetail[];
}

interface ListResponse {
  versions: VersionSummary[];
  active: Record<Kind, string>;
}

export function PromptVersionsEditor({
  token, fetchFn = adminFetch,
}: { token: string; fetchFn?: typeof adminFetch }) {
  const [kind, setKind] = useState<Kind>('system');
  const [kinds, setKinds] = useState<KindMeta[]>([]);
  const [kindModalOpen, setKindModalOpen] = useState(false);
  const [kindDraft, setKindDraft] = useState({ key: '', label: '' });
  const [items, setItems] = useState<VersionSummary[]>([]);
  const [active, setActive] = useState<Record<Kind, string>>({});
  const [loading, setLoading] = useState(false);
  const [desktopPreview, setDesktopPreview] = useState<PromptPreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const previewDesktop = async (row: VersionSummary, parts?: PartDetail[]) => {
    const endpoint = kinds.find(k => k.key === row.kind)?.preview;
    if (!endpoint) return;
    setPreviewLoading(true);
    try {
      const res = await fetchFn(token, `/v1/admin/prompts/${endpoint}`, {
        method: 'POST', body: JSON.stringify({ version_id: row.id, parts }),
      });
      setDesktopPreview({ ...res.data, draft: !!parts });
    } catch (e) { message.error((e as Error).message); }
    finally { setPreviewLoading(false); }
  };

  const [editOpen, setEditOpen] = useState(false);
  const [editing, setEditing] = useState<VersionDetail | null>(null);
  const [editLoading, setEditLoading] = useState(false);

  const [createOpen, setCreateOpen] = useState(false);
  const [createForm] = Form.useForm();

  // Action landing feedback: after moving a fragment up/down in the drawer, highlight the landing card (by its sorted index)
  const { flashKey: movedPartKey, flash: flashMovedPart } = useFlashKey();

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchFn(token, `/v1/admin/prompts/versions?kind=${kind}`);
      const empty: Record<Kind, string> = {};
      const data: ListResponse = res.data || { versions: [], active: empty };
      setItems(data.versions || []);
      setActive({ ...empty, ...(data.active || {}) });
    } catch (e) {
      message.error(t('加载失败：{msg}', { msg: (e as Error).message }));
    } finally {
      setLoading(false);
    }
  }, [kind, token, fetchFn]);

  useEffect(() => { reload(); }, [reload]);

  /** tab 清单：内置 + 管理员自建。新建/删除 tab 后重取。 */
  const loadKinds = useCallback(async () => {
    try {
      const res = await fetchFn(token, '/v1/admin/prompts/kinds');
      setKinds(((res.data as { kinds?: KindMeta[] })?.kinds) || []);
    } catch { /* 拿不到就只剩内置 tab（下方 visibleKinds 有兜底） */ }
  }, [token, fetchFn]);
  useEffect(() => { void loadKinds(); }, [loadKinds]);

  const visibleKinds = useMemo<KindMeta[]>(() => {
    const list = kinds.length
      ? kinds
      : [{ key: 'system', label: t('系统提示词'), builtin: true }];
    return list.filter((k) => k.visible !== false);
  }, [kinds]);

  const createKind = async () => {
    const key = kindDraft.key.trim();
    if (!key) { message.warning(t('请填写标识')); return; }
    try {
      await fetchFn(token, '/v1/admin/prompts/kinds', {
        method: 'POST',
        body: JSON.stringify({ key, label: kindDraft.label.trim() || key }),
      });
      message.success(t('已新建提示词分类'));
      setKindModalOpen(false);
      setKindDraft({ key: '', label: '' });
      await loadKinds();
      setKind(key);
    } catch (e) {
      message.error(t('新建失败：{msg}', { msg: (e as Error).message }));
    }
  };

  const removeKind = (meta: KindMeta) => {
    Modal.confirm({
      title: t('删除分类「{name}」？', { name: meta.label }),
      content: t('该分类下的全部版本会一并删除；绑定了它的对话模式会退回默认提示词装配。'),
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await fetchFn(token, `/v1/admin/prompts/kinds/${encodeURIComponent(meta.key)}`, { method: 'DELETE' });
          message.success(t('已删除'));
          await loadKinds();
          setKind('system');
        } catch (e) {
          message.error(t('删除失败：{msg}', { msg: (e as Error).message }));
        }
      },
    });
  };

  // ── Open editor ────────────────────────────────────────────────
  const openEdit = async (row: VersionSummary) => {
    setEditOpen(true);
    setEditLoading(true);
    setEditing(null);
    try {
      const res = await fetchFn(token, `/v1/admin/prompts/versions/${row.kind}/${row.id}`);
      setEditing(res.data as VersionDetail);
    } catch (e) {
      message.error(t('加载版本详情失败：{msg}', { msg: (e as Error).message }));
      setEditOpen(false);
    } finally {
      setEditLoading(false);
    }
  };

  const handleSave = async () => {
    if (!editing) return;
    try {
      await fetchFn(token, `/v1/admin/prompts/versions/${editing.kind}/${editing.id}`, {
        method: 'PUT',
        body: JSON.stringify({
          id: editing.id,
          kind: editing.kind,
          name: editing.name,
          description: editing.description,
          parts: editing.parts,
        }),
      });
      message.success(t('版本已保存'));
      setEditOpen(false);
      setEditing(null);
      reload();
    } catch (e) {
      message.error(t('保存失败：{msg}', { msg: (e as Error).message }));
    }
  };

  const handleActivate = async (row: VersionSummary) => {
    try {
      await fetchFn(token, `/v1/admin/prompts/versions/${row.kind}/${row.id}/activate`, { method: 'POST' });
      message.success(t('已激活：{name}', { name: row.name }));
      reload();
    } catch (e) {
      message.error(t('激活失败：{msg}', { msg: (e as Error).message }));
    }
  };

  const handleDelete = async (row: VersionSummary) => {
    try {
      await fetchFn(token, `/v1/admin/prompts/versions/${row.kind}/${row.id}`, { method: 'DELETE' });
      message.success(t('已删除'));
      reload();
    } catch (e) {
      message.error(t('删除失败：{msg}', { msg: (e as Error).message }));
    }
  };

  const handleClone = async (row: VersionSummary) => {
    const newId = prompt(t('克隆自 {id}，请输入新版本 id', { id: row.id }), `${row.id}_copy`);
    if (!newId) return;
    try {
      await fetchFn(token, '/v1/admin/prompts/versions', {
        method: 'POST',
        body: JSON.stringify({
          id: newId,
          kind: row.kind,
          name: `${row.name} ${t('副本')}`,
          description: row.description,
          from_id: row.id,
        }),
      });
      message.success(t('已克隆'));
      reload();
    } catch (e) {
      message.error(t('克隆失败：{msg}', { msg: (e as Error).message }));
    }
  };

  // ── Create new version ─────────────────────────────────────────
  const handleCreate = async () => {
    try {
      const values = await createForm.validateFields();
      const body: Record<string, unknown> = {
        id: values.id,
        kind,
        name: values.name || values.id,
        description: values.description || '',
      };
      if (values.from_id) {
        // Clone: let the backend copy parts from from_id, don't pass parts:[] which would overwrite them
        body.from_id = values.from_id;
      } else {
        body.parts = [];
      }
      await fetchFn(token, '/v1/admin/prompts/versions', {
        method: 'POST',
        body: JSON.stringify(body),
      });
      message.success(t('新版本已创建'));
      setCreateOpen(false);
      createForm.resetFields();
      reload();
    } catch (e) {
      if (e && typeof e === 'object' && 'message' in e) {
        message.error(t('创建失败：{msg}', { msg: (e as Error).message }));
      }
    }
  };

  // ── Seed (reload defaults from code) ───────────────────────────
  const handleSeed = async () => {
    try {
      await fetchFn(token, '/v1/admin/prompts/versions/seed', {
        method: 'POST',
      });
      message.success(t('已从代码重建默认版本（仅补齐缺失项）'));
      reload();
    } catch (e) {
      message.error(t('重建失败：{msg}', { msg: (e as Error).message }));
    }
  };

  // ── Export / Import snapshot ───────────────────────────────────
  // The snapshot overwrites the entire prompt version pool (all kinds: system / subagents / plan_mode / code_exec / distillation
  // + activation mapping) and the prompt hub, independent of the current Tab.

  const handleExport = async () => {
    try {
      const res = await fetchFn(token, '/v1/admin/prompts/snapshot');
      const blob = new Blob([JSON.stringify(res.data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `prompt-snapshot-${formatDateKey(new Date())}.json`;
      a.click();
      URL.revokeObjectURL(url);
      message.success(t('提示词快照已导出（版本池 + 提示词广场）'));
    } catch (e) {
      message.error(t('导出失败：{msg}', { msg: (e as Error).message }));
    }
  };

  const handleImport = (file: File) => {
    (async () => {
      let snapshot: unknown;
      try {
        snapshot = JSON.parse(await file.text());
      } catch {
        message.error(t('文件解析失败：不是合法的 JSON'));
        return;
      }
      if (!snapshot || typeof snapshot !== 'object' || !('blocks' in (snapshot as object))) {
        message.error(t('文件格式错误：缺少 blocks 字段，请使用「导出」生成的快照文件'));
        return;
      }
      Modal.confirm({
        title: t('导入提示词快照？'),
        content: t('将用快照覆盖当前的提示词版本池与提示词广场，并清除提示词缓存使其立即生效。此操作不可撤销，建议先「导出」当前配置作为备份。'),
        okText: t('确认导入'),
        okButtonProps: { danger: true },
        cancelText: t('取消'),
        onOk: async () => {
          try {
            const res = await fetchFn(token, '/v1/admin/prompts/snapshot', {
              method: 'POST',
              body: JSON.stringify(snapshot),
            });
            const imported = (res.data?.imported || []) as string[];
            message.success(t('导入成功（已写入：{list}）', { list: imported.length ? imported.join('、') : t('无') }));
            reload();
          } catch (e) {
            message.error(t('导入失败：{msg}', { msg: (e as Error).message }));
          }
        },
      });
    })();
    return false; // prevent antd Upload from auto-uploading
  };

  // ── Parts editor (inside version drawer) ───────────────────────
  const updatePart = (idx: number, patch: Partial<PartDetail>) => {
    if (!editing) return;
    const parts = [...editing.parts];
    parts[idx] = { ...parts[idx], ...patch };
    setEditing({ ...editing, parts });
  };

  const removePart = (idx: number) => {
    if (!editing) return;
    const parts = editing.parts.filter((_, i) => i !== idx);
    setEditing({ ...editing, parts });
  };

  const addPart = () => {
    if (!editing) return;
    const nextOrder = editing.parts.length === 0 ? 0
      : Math.max(...editing.parts.map((p) => p.sort_order)) + 10;
    setEditing({
      ...editing,
      parts: [
        ...editing.parts,
        {
          part_id: `system/${String(Math.floor(nextOrder / 10)).padStart(2, '0')}_custom`,
          display_name: t('自定义模块'),
          content: '',
          sort_order: nextOrder,
          is_enabled: true,
        },
      ],
    });
  };

  const movePart = (idx: number, direction: 'up' | 'down') => {
    if (!editing) return;
    const swapIdx = direction === 'up' ? idx - 1 : idx + 1;
    if (swapIdx < 0 || swapIdx >= editing.parts.length) return;
    const parts = [...editing.parts];
    const a = parts[idx];
    const b = parts[swapIdx];
    const tmp = a.sort_order;
    a.sort_order = b.sort_order;
    b.sort_order = tmp;
    parts.sort((x, y) => x.sort_order - y.sort_order);
    setEditing({ ...editing, parts });
    flashMovedPart(String(parts.indexOf(a)));
  };

  // ── Columns ────────────────────────────────────────────────────
  const columns = useMemo(() => [
    {
      title: t('版本 ID'),
      dataIndex: 'id',
      key: 'id',
      width: 160,
      render: (id: string, row: VersionSummary) => (
        <Space>
          <code>{id}</code>
          {row.is_active && (
            <Tag color="green" icon={<CheckCircleTwoTone twoToneColor="#52c41a" />}>
              {t('激活中')}
            </Tag>
          )}
        </Space>
      ),
    },
    { title: t('名称'), dataIndex: 'name', key: 'name', width: 240 },
    {
      title: t('描述'), dataIndex: 'description', key: 'description',
      ellipsis: true,
      render: (v: string) => v || <span style={{ color: 'var(--color-text-tertiary)' }}>—</span>,
    },
    {
      title: t('片段数'), dataIndex: 'parts_count', key: 'parts_count', width: 80,
      render: (v: number) => t('{n} 个', { n: v }),
    },
    {
      title: t('更新时间'), dataIndex: 'updated_at', key: 'updated_at', width: 170,
      render: (v: string | null) => formatDateTime(v, '-'),
    },
    {
      title: t('操作'), key: 'action', width: 340,
      render: (_: unknown, row: VersionSummary) => (
        <Space size="small">
          {kinds.find(k => k.key === row.kind)?.preview && <Button type="link" size="small" loading={previewLoading} onClick={() => previewDesktop(row)}>{t('完整预览')}</Button>}
          <Button type="link" size="small" icon={<EditOutlined />} onClick={() => openEdit(row)}>
            {t('编辑')}
          </Button>
          <Button type="link" size="small" icon={<CopyOutlined />} onClick={() => handleClone(row)}>
            {t('克隆')}
          </Button>
          {!row.is_active && (
            <Popconfirm
              title={t('确认激活 "{name}"？', { name: row.name })}
              description={t('激活后该版本立即生效，并清除当前提示词缓存。')}
              onConfirm={() => handleActivate(row)}
            >
              <Button type="link" size="small" icon={<ThunderboltOutlined />}>
                {t('激活')}
              </Button>
            </Popconfirm>
          )}
          {!row.is_active && (
            <Popconfirm title={t('删除版本 "{id}"？不可恢复。', { id: row.id })} onConfirm={() => handleDelete(row)}>
              <Button type="link" size="small" danger icon={<DeleteOutlined />}>
                {t('删除')}
              </Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ], [active, token, fetchFn, previewLoading, kinds]);

  const { columns: resizableColumns, components: resizableComponents, tableProps } =
    useResizableColumns(columns, { storageKey: 'admin-prompt-versions' });

  return (
    <>
      <PromptPreviewModal data={desktopPreview} onClose={() => setDesktopPreview(null)} />
      <Tabs
        activeKey={kind}
        onChange={(k) => setKind(k as Kind)}
        items={visibleKinds.map((k) => ({
          key: k.key,
          label: (
            <span>
              {k.builtin ? t(k.label) : k.label}
              {active[k.key] && <Tag color="blue" style={{ marginLeft: 8 }}>{active[k.key]}</Tag>}
              {!k.builtin && (
                <DeleteOutlined
                  style={{ marginLeft: 8, opacity: 0.5 }}
                  onClick={(e) => { e.stopPropagation(); removeKind(k); }}
                />
              )}
            </span>
          ),
        }))}
        tabBarExtraContent={(
          <Button size="small" icon={<PlusOutlined />} onClick={() => setKindModalOpen(true)}>
            {t('新建分类')}
          </Button>
        )}
      />

      <Space style={{ marginBottom: 16 }} wrap>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
          {t('新建版本')}
        </Button>
        <Popconfirm
          title={t('从代码重建默认版本？')}
          description={t('仅补齐缺失的默认版本。已存在的版本不会被覆盖。')}
          onConfirm={handleSeed}
        >
          <Button icon={<RollbackOutlined />}>{t('从代码重建默认')}</Button>
        </Popconfirm>
        <Tooltip title={t('导出全部提示词版本池（含所有 kind 与激活映射）与提示词广场为 JSON 快照')}>
          <Button icon={<ExportOutlined />} onClick={handleExport}>{t('导出')}</Button>
        </Tooltip>
        <Tooltip title={t('从 JSON 快照导入并覆盖提示词版本池与提示词广场')}>
          <Upload accept=".json" showUploadList={false} beforeUpload={handleImport}>
            <Button icon={<ImportOutlined />}>{t('导入')}</Button>
          </Upload>
        </Tooltip>
        <Button icon={<ReloadOutlined />} onClick={reload}>{t('刷新')}</Button>
      </Space>

      <Table
        scroll={{ x: 1100 }}
        rowKey="id"
        columns={resizableColumns}
        components={resizableComponents}
        {...tableProps}
        dataSource={items}
        loading={loading}
        pagination={false}
        size="middle"
      />

      {/* Edit Drawer */}
      <Drawer
        title={editing ? t('编辑版本: {kind}/{id}', { kind: editing.kind, id: editing.id }) : t('加载中...')}
        width={900}
        open={editOpen}
        onClose={() => { setEditOpen(false); setEditing(null); }}
        extra={
          <Space>
            <Button onClick={() => { setEditOpen(false); setEditing(null); }}>{t('取消')}</Button>
            {editing && kinds.find(k => k.key === editing.kind)?.preview && <Button loading={previewLoading} onClick={() => previewDesktop(editing, editing.parts)}>{t('预览当前编辑')}</Button>}
            <Button type="primary" disabled={!editing} onClick={handleSave}>{t('保存')}</Button>
          </Space>
        }
      >
        {editLoading || !editing ? (
          <div style={{ padding: 40, textAlign: 'center' }}>{t('加载中...')}</div>
        ) : (
          /* Two-stage load: once details are ready, the whole content fades in over 0.18s */
          <div className="admin-fade-in">
            <Form layout="vertical">
              <Form.Item label={t('名称')}>
                <Input value={editing.name} onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
              </Form.Item>
              <Form.Item label={t('描述')}>
                <Input.TextArea
                  rows={2}
                  value={editing.description}
                  onChange={(e) => setEditing({ ...editing, description: e.target.value })}
                />
              </Form.Item>
            </Form>

            <h4 style={{ marginTop: 16 }}>{t('提示词片段 ({n})', { n: editing.parts.length })}</h4>
            <Space style={{ marginBottom: 8 }}>
              <Button size="small" icon={<PlusOutlined />} onClick={addPart}>{t('新增片段')}</Button>
            </Space>
            {editing.parts.map((p, idx) => (
              <div
                key={idx}
                className={movedPartKey === String(idx) ? 'admin-flash-once' : undefined}
                style={{
                  background: 'var(--color-bg-gray)', padding: 12, marginBottom: 12, borderRadius: 4,
                }}
              >
                <Space style={{ marginBottom: 8 }} wrap>
                  <Input
                    size="small"
                    style={{ width: 280 }}
                    placeholder="part_id (e.g. system/00_role)"
                    value={p.part_id}
                    onChange={(e) => updatePart(idx, { part_id: e.target.value })}
                  />
                  <Input
                    size="small"
                    style={{ width: 160 }}
                    placeholder={t('显示名称')}
                    value={p.display_name}
                    onChange={(e) => updatePart(idx, { display_name: e.target.value })}
                  />
                  <Input
                    size="small"
                    type="number"
                    style={{ width: 90 }}
                    value={p.sort_order}
                    onChange={(e) => updatePart(idx, { sort_order: Number(e.target.value) || 0 })}
                  />
                  <Switch
                    size="small"
                    checked={p.is_enabled}
                    onChange={(v) => updatePart(idx, { is_enabled: v })}
                  />
                  <Button size="small" icon={<ArrowUpOutlined />} disabled={idx === 0}
                    onClick={() => movePart(idx, 'up')} />
                  <Button size="small" icon={<ArrowDownOutlined />}
                    disabled={idx === editing.parts.length - 1}
                    onClick={() => movePart(idx, 'down')} />
                  <Popconfirm title={t('删除此片段？')} onConfirm={() => removePart(idx)}>
                    <Button size="small" danger icon={<DeleteOutlined />} />
                  </Popconfirm>
                </Space>
                <Input.TextArea
                  rows={8}
                  style={{ fontFamily: 'monospace', fontSize: 12 }}
                  value={p.content}
                  onChange={(e) => updatePart(idx, { content: e.target.value })}
                />
              </div>
            ))}
          </div>
        )}
      </Drawer>

      {/* Create Modal */}
      <Modal
        title={t('新建版本（{kind}）', { kind: t(kinds.find(k => k.key === kind)?.label || kind) })}
        open={createOpen}
        onCancel={() => { setCreateOpen(false); createForm.resetFields(); }}
        onOk={handleCreate}
        okText={t('创建')}
        cancelText={t('取消')}
      >
        <Form form={createForm} layout="vertical">
          <Form.Item
            name="id"
            label={t('版本 ID')}
            rules={[{ required: true, message: t('请输入版本 id（同 kind 内唯一）') }]}
          >
            <Input placeholder="v5 / custom_a" />
          </Form.Item>
          <Form.Item name="name" label={t('名称')}>
            <Input placeholder={t('版本名称')} />
          </Form.Item>
          <Form.Item name="description" label={t('描述')}>
            <Input.TextArea rows={2} placeholder={t('简要描述这个版本的目的或差异')} />
          </Form.Item>
          <Form.Item name="from_id" label={t('基于现有版本克隆（可选）')}>
            <Select
              allowClear
              placeholder={t('不选则创建空版本')}
              options={items.map((v) => ({ label: `${v.id} (${v.name})`, value: v.id }))}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        open={kindModalOpen}
        title={t('新建提示词分类')}
        onCancel={() => setKindModalOpen(false)}
        onOk={createKind}
        destroyOnClose
      >
        <p style={{ color: 'var(--color-text-tertiary)', fontSize: 13 }}>
          {t('新建后会自动带一个可直接编辑的空版本。「模式选择」里的模式可以绑定这个分类，把它作为该模式的专属提示词。')}
        </p>
        <Form layout="vertical">
          <Form.Item label={t('标识')} required help={t('英文小写、数字、- 和 _；落到接口与模式绑定上')}>
            <Input
              value={kindDraft.key}
              onChange={(e) => setKindDraft((d) => ({ ...d, key: e.target.value }))}
              placeholder="research_report"
              maxLength={64}
            />
          </Form.Item>
          <Form.Item label={t('显示名')} help={t('tab 上显示的字，留空则用标识')}>
            <Input
              value={kindDraft.label}
              onChange={(e) => setKindDraft((d) => ({ ...d, label: e.target.value }))}
              placeholder={t('例如：研报模式')}
              maxLength={60}
            />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}

export default PromptVersionsEditor;
