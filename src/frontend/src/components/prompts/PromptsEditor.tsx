import { useState, useEffect, useCallback } from 'react';
import { motion } from 'motion/react';
import {
  Button, Drawer, Form, Input, Modal, Popconfirm,
  Space, Switch, Table, Tabs, Tag, Upload, Select, message,
} from 'antd';
import {
  PlusOutlined, EyeOutlined, EditOutlined, DeleteOutlined,
  ArrowUpOutlined, ArrowDownOutlined,
  ExportOutlined, ImportOutlined,
} from '@ant-design/icons';
import { promptFetch as adminFetch } from './promptApi';
import { formatDateKey } from '../../utils/date';
import { PromptVersionsEditor } from './PromptVersionsEditor';
import { useFlashKey } from '../../hooks/useFlash';
import { useResizableColumns } from '../common/ResizableColumns';
import { t, tCtx } from '../../i18n';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { LOCAL_TARGET_HEADER, type RequestTarget } from '../../api';

interface PromptPart {
  part_id: string;
  content: string;
  display_name: string;
  sort_order: number;
  is_enabled: boolean;
  source: string;
  updated_at?: string;
  created_by?: string;
}

function PromptEditorContent({ token, fetchFn = adminFetch }: { token: string; fetchFn?: typeof adminFetch }) {
  const [topTab, setTopTab] = useState<'parts' | 'versions'>('parts');
  const [parts, setParts] = useState<PromptPart[]>([]);
  const [loading, setLoading] = useState(true);
  const [editOpen, setEditOpen] = useState(false);
  const [editingPart, setEditingPart] = useState<PromptPart | null>(null);
  const [fsContent, setFsContent] = useState<string | null>(null);
  const [form] = Form.useForm();
  const [createOpen, setCreateOpen] = useState(false);
  const [createForm] = Form.useForm();
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewContent, setPreviewContent] = useState('');
  const [previewLoading, setPreviewLoading] = useState(false);

  const [activeInfo, setActiveInfo] = useState<{ id: string; name: string; parts_count: number } | null>(null);

  // Action-landing feedback: after moving a row up/down, highlight the moved row (rowKey=part_id)
  const { flashKey: movedKey, flash: flashMoved } = useFlashKey();

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const [partsRes, activeRes] = await Promise.all([
        fetchFn(token, '/v1/admin/prompts/parts'),
        fetchFn(token, '/v1/admin/prompts/active').catch(() => ({ data: {} })),
      ]);
      setParts(partsRes.data || []);
      const sysActive = (activeRes.data || {}).system;
      if (sysActive) setActiveInfo(sysActive);
    } catch (e) {
      message.error(t('加载失败：{msg}', { msg: (e as Error).message }));
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    if (topTab === 'parts') reload();
  }, [topTab, reload]);

  // ── Edit ────────────────────────────────────────────────────────

  const openEdit = async (record: PromptPart) => {
    setEditingPart(record);
    setEditOpen(true);
    setFsContent(null);
    try {
      const res = await fetchFn(token, `/v1/admin/prompts/parts/${record.part_id}`);
      const d = res.data;
      form.setFieldsValue({
        content: d.current.content,
        display_name: d.current.display_name,
        sort_order: d.current.sort_order,
        is_enabled: d.current.is_enabled,
      });
      setFsContent(d.filesystem_content);
    } catch (e) {
      message.error(t('加载详情失败：{msg}', { msg: (e as Error).message }));
    }
  };

  const handleSave = async () => {
    if (!editingPart) return;
    try {
      const values = await form.validateFields();
      await fetchFn(token, `/v1/admin/prompts/parts/${editingPart.part_id}`, {
        method: 'PUT',
        body: JSON.stringify(values),
      });
      message.success(t('保存成功'));
      setEditOpen(false);
      setEditingPart(null);
      form.resetFields();
      reload();
    } catch (e) {
      if (e && typeof e === 'object' && 'message' in e) {
        message.error(t('保存失败：{msg}', { msg: (e as Error).message }));
      }
    }
  };

  // ── Create ────────────────────────────────────────────────────────

  const handleCreate = async () => {
    try {
      const values = await createForm.validateFields();
      const partId = values.part_id;
      await fetchFn(token, `/v1/admin/prompts/parts/${partId}`, {
        method: 'PUT',
        body: JSON.stringify({
          content: values.content,
          display_name: values.display_name,
          sort_order: values.sort_order ?? 99,
          is_enabled: true,
        }),
      });
      message.success(t('模块创建成功'));
      setCreateOpen(false);
      createForm.resetFields();
      reload();
    } catch (e) {
      if (e && typeof e === 'object' && 'message' in e) {
        message.error(t('创建失败：{msg}', { msg: (e as Error).message }));
      }
    }
  };

  // ── Delete (restore filesystem) ──────────────────────────────────

  const handleDelete = async (partId: string) => {
    try {
      await fetchFn(token, `/v1/admin/prompts/parts/${partId}`, { method: 'DELETE' });
      message.success(t('已恢复为文件系统版本'));
      reload();
    } catch (e) {
      message.error(t('删除失败：{msg}', { msg: (e as Error).message }));
    }
  };

  // ── Toggle enabled ────────────────────────────────────────────────

  const handleToggle = async (record: PromptPart, enabled: boolean) => {
    try {
      await fetchFn(token, `/v1/admin/prompts/parts/${record.part_id}`, {
        method: 'PUT',
        body: JSON.stringify({
          content: record.content,
          display_name: record.display_name,
          sort_order: record.sort_order,
          is_enabled: enabled,
        }),
      });
      message.success(enabled ? t('已启用') : t('已禁用'));
      reload();
    } catch (e) {
      message.error(t('操作失败：{msg}', { msg: (e as Error).message }));
    }
  };

  // ── Move (reorder) ────────────────────────────────────────────────

  const handleMove = async (index: number, direction: 'up' | 'down') => {
    const swapIndex = direction === 'up' ? index - 1 : index + 1;
    if (swapIndex < 0 || swapIndex >= parts.length) return;

    const newParts = [...parts];
    const a = newParts[index];
    const b = newParts[swapIndex];

    // Swap sort_order values
    const tempOrder = a.sort_order;
    a.sort_order = b.sort_order;
    b.sort_order = tempOrder;

    // Save both to DB
    try {
      await fetchFn(token, '/v1/admin/prompts/order', {
        method: 'PUT',
        body: JSON.stringify({
          order: [
            { part_id: a.part_id, sort_order: a.sort_order },
            { part_id: b.part_id, sort_order: b.sort_order },
          ],
        }),
      });
      // Only DB-sourced parts can be reordered via the order API,
      // but we also need to ensure both are saved to DB first.
      // For file-only parts, we need to save them first.
      if (a.source === 'file') {
        await fetchFn(token, `/v1/admin/prompts/parts/${a.part_id}`, {
          method: 'PUT',
          body: JSON.stringify({
            content: a.content,
            display_name: a.display_name,
            sort_order: a.sort_order,
            is_enabled: a.is_enabled,
          }),
        });
      }
      if (b.source === 'file') {
        await fetchFn(token, `/v1/admin/prompts/parts/${b.part_id}`, {
          method: 'PUT',
          body: JSON.stringify({
            content: b.content,
            display_name: b.display_name,
            sort_order: b.sort_order,
            is_enabled: b.is_enabled,
          }),
        });
      }
      await reload();
      flashMoved(a.part_id);
    } catch (e) {
      message.error(t('排序失败：{msg}', { msg: (e as Error).message }));
    }
  };

  // ── Export / Import ─────────────────────────────────────────────

  const handleExport = async () => {
    try {
      const res = await fetchFn(token, '/v1/admin/prompts/export');
      const blob = new Blob([JSON.stringify(res.data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `prompts-${formatDateKey(new Date())}.json`;
      a.click();
      URL.revokeObjectURL(url);
      message.success(t('提示词配置已导出'));
    } catch (e) {
      message.error(t('导出失败：{msg}', { msg: (e as Error).message }));
    }
  };

  const handleImportJson = async (file: File) => {
    try {
      const text = await file.text();
      const data = JSON.parse(text);
      if (!Array.isArray(data)) { message.error(t('JSON 格式错误：需要数组')); return false; }
      await fetchFn(token, '/v1/admin/prompts/import', {
        method: 'POST',
        body: JSON.stringify({ parts: data, overwrite: true }),
      });
      message.success(t('提示词配置已导入'));
      reload();
    } catch (e) {
      message.error(t('导入失败：{msg}', { msg: (e as Error).message }));
    }
    return false;
  };

  // ── Preview ────────────────────────────────────────────────────────

  const handlePreview = async () => {
    setPreviewOpen(true);
    setPreviewLoading(true);
    try {
      const res = await fetchFn(token, '/v1/admin/prompts/preview', {
        method: 'POST',
      });
      setPreviewContent(res.data.prompt || '');
    } catch (e) {
      message.error(t('预览失败：{msg}', { msg: (e as Error).message }));
    } finally {
      setPreviewLoading(false);
    }
  };

  // ── Table columns ──────────────────────────────────────────────────

  const columns = [
    {
      title: t('排序'),
      dataIndex: 'sort_order',
      key: 'sort_order',
      width: 70,
    },
    {
      title: 'Part ID',
      dataIndex: 'part_id',
      key: 'part_id',
      width: 200,
      ellipsis: true,
    },
    {
      title: t('名称'),
      dataIndex: 'display_name',
      key: 'display_name',
      width: 150,
    },
    {
      title: t('来源'),
      dataIndex: 'source',
      key: 'source',
      width: 90,
      render: (source: string) => (
        <Tag color={source === 'database' ? 'orange' : 'blue'}>
          {source === 'database' ? t('数据库') : t('文件')}
        </Tag>
      ),
    },
    {
      title: t('启用'),
      key: 'is_enabled',
      width: 80,
      render: (_: unknown, record: PromptPart) => (
        <Switch
          size="small"
          checked={record.is_enabled}
          onChange={(checked) => handleToggle(record, checked)}
        />
      ),
    },
    {
      title: t('操作'),
      key: 'action',
      width: 240,
      render: (_: unknown, record: PromptPart, index: number) => (
        <Space size="small">
          <Button
            type="link"
            size="small"
            icon={<ArrowUpOutlined />}
            disabled={index === 0}
            onClick={() => handleMove(index, 'up')}
          />
          <Button
            type="link"
            size="small"
            icon={<ArrowDownOutlined />}
            disabled={index === parts.length - 1}
            onClick={() => handleMove(index, 'down')}
          />
          <Button
            type="link"
            size="small"
            icon={<EditOutlined />}
            onClick={() => openEdit(record)}
          >
            {t('编辑')}
          </Button>
          {record.source === 'database' && (
            <Popconfirm
              title={t('删除数据库覆盖，恢复为文件系统版本？')}
              onConfirm={() => handleDelete(record.part_id)}
            >
              <Button type="link" size="small" danger icon={<DeleteOutlined />}>
                {tCtx('restore', '恢复')}
              </Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  const { columns: resizableColumns, components: resizableComponents, tableProps } =
    useResizableColumns(columns, { storageKey: 'admin-prompts' });

  return (
    <>
      <Tabs
        activeKey={topTab}
        onChange={(k) => setTopTab(k as 'parts' | 'versions')}
        items={[
          { key: 'parts', label: t('当前版本片段编辑') },
          { key: 'versions', label: t('版本管理') },
        ]}
        style={{ marginBottom: 8 }}
      />
      {topTab === 'versions' ? (
        <PromptVersionsEditor token={token} fetchFn={fetchFn} />
      ) : (
        <>
      {activeInfo && (
        <div style={{
          background: 'var(--color-bg-gray)', border: '1px solid var(--color-border)', borderRadius: 4,
          padding: '8px 12px', marginBottom: 12, fontSize: 13,
        }}>
          {t('正在编辑系统提示词版本：')}
          <Tag color="green" style={{ marginLeft: 8 }}>{activeInfo.id}</Tag>
          <span style={{ color: 'var(--color-text-tertiary)' }}>
            {activeInfo.name} · {t('{n} 个片段', { n: activeInfo.parts_count })}
          </span>
          <span style={{ color: 'var(--color-text-tertiary)', marginLeft: 12 }}>
            {t('要切换版本，请打开「版本管理」Tab')}
          </span>
        </div>
      )}
      <Space style={{ marginBottom: 16 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
          {t('新增模块')}
        </Button>
        <Button icon={<EyeOutlined />} onClick={handlePreview}>
          {t('预览完整提示词')}
        </Button>
        <Button icon={<ExportOutlined />} onClick={handleExport}>{t('导出')}</Button>
        <Upload accept=".json" showUploadList={false} beforeUpload={handleImportJson}>
          <Button icon={<ImportOutlined />}>{t('导入')}</Button>
        </Upload>
      </Space>

      <Table
        rowKey="part_id"
        rowClassName={(record) => (record.part_id === movedKey ? 'row-just-moved' : '')}
        columns={resizableColumns}
        components={resizableComponents}
        {...tableProps}
        dataSource={parts}
        loading={loading}
        pagination={false}
        size="middle"
      />

      {/* Edit Drawer — with Tabs for editing + version history */}
      <Drawer
        title={t('编辑提示词模块: {id}', { id: editingPart?.part_id || '' })}
        open={editOpen}
        onClose={() => {
          setEditOpen(false);
          setEditingPart(null);
          form.resetFields();
        }}
        width={750}
        extra={
          <Button type="primary" onClick={handleSave}>{t('保存')}</Button>
        }
      >
        <Tabs
          defaultActiveKey="edit"
          items={[
            {
              key: 'edit',
              label: t('编辑内容'),
              children: (
                <>
                  <Form form={form} layout="vertical">
                    <Form.Item
                      name="display_name"
                      label={t('显示名称')}
                      rules={[{ required: true, message: t('请输入显示名称') }]}
                    >
                      <Input placeholder={t('角色定义')} />
                    </Form.Item>
                    <Form.Item name="sort_order" label={t('排序')}>
                      <Input type="number" placeholder="0" />
                    </Form.Item>
                    <Form.Item name="is_enabled" label={t('启用')} valuePropName="checked">
                      <Switch />
                    </Form.Item>
                    <Form.Item
                      name="content"
                      label={t('内容 (Markdown)')}
                      rules={[{ required: true, message: t('请输入内容') }]}
                    >
                      <Input.TextArea
                        rows={18}
                        style={{ fontFamily: 'monospace', fontSize: 13 }}
                        placeholder={t('# 系统提示词模块内容...')}
                      />
                    </Form.Item>
                  </Form>
                  {fsContent !== null && (
                    /* Async-loaded block expands via height 0→auto, to prevent the drawer content from jumping */
                    <motion.div
                      initial={{ height: 0, opacity: 0 }}
                      animate={{ height: 'auto', opacity: 1 }}
                      transition={{ duration: 0.25, ease: 'easeInOut' }}
                      style={{ overflow: 'hidden' }}
                    >
                    <div style={{ marginTop: 16 }}>
                      <h4 style={{ color: 'var(--color-text-tertiary)' }}>{t('文件系统原版内容（只读参考）')}</h4>
                      <pre style={{
                        background: 'var(--color-bg-gray)',
                        padding: 12,
                        borderRadius: 4,
                        whiteSpace: 'pre-wrap',
                        fontSize: 12,
                        maxHeight: 300,
                        overflow: 'auto',
                      }}>
                        {fsContent}
                      </pre>
                    </div>
                    </motion.div>
                  )}
                </>
              ),
            },
          ]}
        />
      </Drawer>

      {/* Create Modal */}
      <Modal
        title={t('新增提示词模块')}
        open={createOpen}
        onCancel={() => { setCreateOpen(false); createForm.resetFields(); }}
        onOk={handleCreate}
        okText={t('创建')}
        cancelText={t('取消')}
        width={640}
      >
        <Form form={createForm} layout="vertical">
          <Form.Item
            name="part_id"
            label="Part ID"
            rules={[{ required: true, message: t('请输入 Part ID (如 system/99_custom)') }]}
          >
            <Input placeholder="system/99_custom" />
          </Form.Item>
          <Form.Item
            name="display_name"
            label={t('显示名称')}
            rules={[{ required: true, message: t('请输入显示名称') }]}
          >
            <Input placeholder={t('自定义模块')} />
          </Form.Item>
          <Form.Item name="sort_order" label={t('排序')} initialValue={99}>
            <Input type="number" />
          </Form.Item>
          <Form.Item
            name="content"
            label={t('内容 (Markdown)')}
            rules={[{ required: true, message: t('请输入内容') }]}
          >
            <Input.TextArea
              rows={10}
              style={{ fontFamily: 'monospace', fontSize: 13 }}
              placeholder="# 模块标题&#10;&#10;内容..."
            />
          </Form.Item>
        </Form>
      </Modal>

      {/* Full Prompt Preview Modal */}
      <Modal
        title={t('完整系统提示词预览')}
        open={previewOpen}
        onCancel={() => setPreviewOpen(false)}
        footer={null}
        width={800}
      >
        {previewLoading ? (
          <div style={{ textAlign: 'center', padding: 40 }}>{t('加载中...')}</div>
        ) : (
          <pre style={{
            background: 'var(--color-bg-gray)',
            padding: 16,
            borderRadius: 4,
            whiteSpace: 'pre-wrap',
            fontSize: 13,
            maxHeight: '70vh',
            overflow: 'auto',
          }}>
            {previewContent}
          </pre>
        )}
      </Modal>

        </>
      )}
    </>
  );
}


/** Keep the whole editor on one backend, including drafts, activation and snapshots. */
export function PromptsEditor({ token, fetchFn = adminFetch }: { token: string; fetchFn?: typeof adminFetch }) {
  const dual = useDeploymentModeStore(state => state.provisionMode === 'dual');
  const activeLocal = useDeploymentModeStore(state => state.activeLocal);
  const [target, setTarget] = useState<RequestTarget>(() => activeLocal ? 'local' : 'cloud');
  const targetedFetch = useCallback<typeof adminFetch>((token, path, options = {}) => {
    const headers = new Headers(options.headers);
    if (dual) headers.set(LOCAL_TARGET_HEADER, target);
    return fetchFn(token, path, { ...options, headers: Object.fromEntries(headers.entries()) });
  }, [fetchFn, dual, target]);
  return <>
    {dual && <Space style={{ marginBottom: 16 }} wrap>
      <span>{t('配置目标')}</span>
      <Select value={target} style={{ minWidth: 160 }} options={[{ value: 'cloud', label: t('云端') }, { value: 'local', label: t('本机') }]}
        onChange={value => Modal.confirm({ title: t('切换配置目标？'), content: t('切换会关闭当前编辑和预览，请先保存修改。'), onOk: () => setTarget(value) })} />
    </Space>}
    <PromptEditorContent key={target} token={token} fetchFn={targetedFetch} />
  </>;
}
