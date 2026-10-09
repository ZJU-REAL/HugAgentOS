import { ApplicationLifecycleControls } from './ApplicationLifecycleControls';
import { useRef, useState } from 'react';
import { Alert, Button, Empty, Input, Modal, Select, Space, Table, Tabs, Tag, Spin, message } from 'antd';
import { authFetch, getApiUrl, LOCAL_TARGET_HEADER } from '../../api';
import { t } from '../../i18n';
import { MCPManagementPanel } from './MCPManagementPanel';
import { applicationRequest, type Target, type RecordRow } from './applicationApi';
import { useApplicationData } from './useApplicationData';

function errorMessage(error: unknown) {
  message.error(error instanceof Error ? error.message : t('操作失败'));
}

export function ApplicationDataPanel({ siteId, applicationId, target = 'cloud', initialTab = 'data',
  editOnOpen = false, onChanged, onBusyChange }: {
  siteId?: string; applicationId?: string; target?: Target; initialTab?: 'data' | 'mcp';
  editOnOpen?: boolean; onChanged?: () => void; onBusyChange?: (busy: boolean) => void;
}) {
  const { available, apps, appId, setAppId, tableName, setTableName, selected, table,
    records, total, page, setPage, loading, error, reload } = useApplicationData(siteId, target, applicationId);
  const [busy, setBusy] = useState(false);
  const [mcpBusy, setMcpBusy] = useState(false);
  const [editor, setEditor] = useState<'table' | 'import' | null>(null);
  const [json, setJson] = useState('');
  const selection = useRef('');
  selection.current = `${target}:${appId}`;

  const openEditor = (kind: 'table' | 'import') => {
    setEditor(kind);
    setJson(JSON.stringify(kind === 'table' ? {
      name: 'entries', public_insert: true,
      columns: [{ name: 'name', type: 'text', required: true }, { name: 'email', type: 'text', required: true }],
    } : { rows: [] }, null, 2));
  };

  const save = async (confirmed = false) => {
    if (!confirmed && editor === 'table') {
      try {
        const definition = JSON.parse(json);
        if (definition.public_replace === true) {
          Modal.confirm({ title: t('允许访客替换整张表'),
            content: t('匿名访客可以清空或替换全部记录。历史仅保留最近20个版本，限流不能防止首次清空。'),
            onOk: () => save(true) });
          return;
        }
      } catch (error) { errorMessage(error); return; }
    }
    if (!selected || !editor) return;
    const mutationSelection = selection.current;
    setBusy(true);
    try {
      const payload: unknown = JSON.parse(json);
      const path = editor === 'table' ? 'tables' : `tables/${tableName}/records`;
      await applicationRequest(`/v1/applications/${appId}/${path}`,
        { method: 'POST', body: JSON.stringify(payload) }, target);
      if (selection.current !== mutationSelection) return;
      setEditor(null);
      await reload();
      onChanged?.();
      message.success(t('已保存'));
    } catch (error) { if (selection.current === mutationSelection) errorMessage(error); }
    finally { setBusy(false); }
  };

  const deleteRecord = (row: RecordRow) => Modal.confirm({
    title: t('删除记录'), content: t('此操作永久删除数据及恢复历史，无法撤销。'),
    okButtonProps: { danger: true },
    onOk: async () => {
      setBusy(true);
      try {
        await applicationRequest('/v1/applications/' + appId + '/tables/' + tableName + '/records/' + row.id + '?version=' + row.version,
          { method: 'DELETE' }, target);
        await reload(); onChanged?.();
      } catch (error) { errorMessage(error); }
      finally { setBusy(false); }
    },
  });

  const exportCSV = async () => {
    try {
      const response = await authFetch(`${getApiUrl()}/v1/applications/${appId}/tables/${tableName}/export`,
        { headers: target === 'local' ? { [LOCAL_TARGET_HEADER]: 'local' } : {} });
      if (!response.ok) throw new Error(t('导出失败'));
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = url; link.download = `${tableName}.csv`; link.click();
      URL.revokeObjectURL(url);
    } catch (error) { errorMessage(error); }
  };

  if (error && !apps.length) return <Alert type="error" message={error}
    action={<Button onClick={() => void reload()}>{t('重试')}</Button>} />;
  if (loading && !apps.length) return <Spin aria-label={t('加载中')} />;
  if (!available) return <Alert type="info" message={t('数据库与 MCP 托管尚未启用')} />;
  if (!apps.length) {
    return <Empty description={siteId ? t('该站点没有数据库；静态网页无需创建数据库') : t('暂无应用数据库或 MCP 服务')} />;
  }

  return <Space orientation="vertical" style={{ width: '100%' }}>
    {error && <Alert type="error" message={error} />}
    {!applicationId && <Select aria-label={t('选择应用')} value={appId} disabled={busy || mcpBusy} onChange={setAppId} style={{ width: '100%' }}
      options={apps.map((app) => ({ value: app.id, label: app.title }))} />}
    <Tabs defaultActiveKey={initialTab} items={[
      { key: 'data', label: t('数据'), children: <Space orientation="vertical" style={{ width: '100%' }}>
        <ApplicationLifecycleControls key={selection.current + tableName} appId={appId} tableName={tableName} target={target}
          disabled={busy || mcpBusy} onBusyChange={setBusy} changed={async () => { await reload(); onChanged?.(); }} />
        <Space wrap>
          <Select aria-label={t('选择数据表')} value={tableName || undefined} disabled={busy || mcpBusy} onChange={(name) => { setTableName(name); setPage(1); }}
            style={{ minWidth: 180 }} options={Object.keys(selected?.tables || {}).map((name) => ({ value: name, label: name }))} />
          <Button onClick={() => openEditor('table')}>{t('定义数据表')}</Button>
          <Button disabled={!table} onClick={() => openEditor('import')}>{t('导入 JSON')}</Button>
          <Button disabled={!table} onClick={() => void exportCSV()}>{t('导出完整 CSV')}</Button>
          <Button onClick={() => void reload().catch(errorMessage)}>{t('刷新')}</Button>
        </Space>
        {table && <>
          {table.public_replace && <Alert type="warning" message={t('匿名访客可以清空或替换全部记录。历史仅保留最近20个版本，限流不能防止首次清空。')} />}
          <Tag color={table.public_insert ? 'blue' : 'default'}>{table.public_insert ? t('访客可提交，管理员可查看') : t('仅管理员访问')}</Tag>
          <Table size="small" rowKey="name" pagination={false} dataSource={table.columns}
            columns={[
              { title: t('字段'), dataIndex: 'name' }, { title: t('类型'), dataIndex: 'type' },
              { title: t('必填'), dataIndex: 'required', render: (value: boolean) => value ? t('是') : t('否') },
              { title: t('唯一'), dataIndex: 'unique', render: (value: boolean) => value ? t('是') : t('否') },
            ]} />
          <Table size="small" rowKey="id" loading={busy || loading} dataSource={records} scroll={{ x: true }}
            columns={[...['id', ...table.columns.map((field) => field.name), 'version', 'created_at'].map((field) => ({
              title: field, dataIndex: field, render: (value: unknown) => value == null ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value),
            })), { title: t('操作'), key: 'delete', render: (_: unknown, row: RecordRow) =>
              <Button size="small" danger disabled={busy || mcpBusy} onClick={() => deleteRecord(row)}>{t('删除记录')}</Button> }]}
            pagination={{ current: page, pageSize: 20, total, onChange: setPage }} />
        </>}
      </Space> },
      { key: 'mcp', label: t('MCP 服务'), children: selected && <MCPManagementPanel
        key={`${target}:${selected.id}`} app={selected} target={target} editOnOpen={editOnOpen}
        onBusyChange={(value) => { setMcpBusy(value); onBusyChange?.(value); }} onChanged={async () => { await reload(); onChanged?.(); }} /> },
    ]} />
    <Modal title={editor === 'table' ? t('定义数据表') : t('导入记录')}
      open={!!editor} closable={!busy} maskClosable={!busy} cancelButtonProps={{ disabled: busy }} onCancel={() => setEditor(null)} onOk={() => void save()} confirmLoading={busy} width={680}>
      <Input.TextArea aria-label={t('JSON 定义')} value={json} onChange={(event) => setJson(event.target.value)} rows={16} />
    </Modal>
  </Space>;
}
