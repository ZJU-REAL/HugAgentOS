import { useCallback, useEffect, useState } from 'react';
import {
  Button,
  Card,
  Descriptions,
  Drawer,
  Segmented,
  Space,
  Table,
  Tabs,
  Tag,
  Timeline,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  getMySkillLog,
  getMySkillLogs,
  getMySubagentLog,
  getMySubagentLogs,
  getMyToolLog,
  getMyToolLogs,
  getMyUsage,
  getMyUsageSummary,
  type MyLogPage,
  type MySkillLogItem,
  type MySubagentLogDetail,
  type MySubagentLogItem,
  type MyToolLogItem,
  type MyUsageItem,
  type MyUsageSummaryItem,
} from '../../api';
import { t } from '../../i18n';

const { Paragraph, Text } = Typography;

type LogKind = 'tools' | 'skills' | 'subagents' | 'usage';
type DetailKind = Exclude<LogKind, 'usage'>;
type LogRow = MyToolLogItem | MySkillLogItem | MySubagentLogItem | MyUsageItem;
type LogDetail =
  | { kind: 'tools'; data: MyToolLogItem }
  | { kind: 'skills'; data: MySkillLogItem }
  | { kind: 'subagents'; data: MySubagentLogDetail };

const PAGE_SIZE = 20;
const STATUS_COLORS: Record<string, string> = {
  running: 'processing',
  success: 'success',
  failed: 'error',
  timeout: 'warning',
  cancelled: 'default',
};
const SOURCE_COLORS: Record<string, string> = {
  main_agent: 'blue',
  subagent: 'purple',
  skill: 'cyan',
  automation: 'gold',
};
const INVOCATION_COLORS: Record<string, string> = {
  view: 'blue',
  run_script: 'green',
  auto_load: 'geekblue',
};

function fmtTime(value?: string | null): string {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleString('zh-CN', { hour12: false });
}

function statusTag(status: string) {
  return <Tag color={STATUS_COLORS[status] || 'default'}>{status}</Tag>;
}

function duration(value?: number | null): string {
  return value != null ? `${value} ms` : '—';
}

function jsonText(value: unknown): string {
  if (value == null) return '—';
  try {
    return JSON.stringify(value, null, 2) ?? '—';
  } catch {
    return String(value);
  }
}

function DetailCard({ title, value, maxHeight = 300 }: {
  title: string;
  value: unknown;
  maxHeight?: number;
}) {
  return (
    <Card size="small" title={title}>
      <pre style={{ maxHeight, overflow: 'auto', margin: 0, whiteSpace: 'pre-wrap' }}>
        {typeof value === 'string' ? value || '—' : jsonText(value)}
      </pre>
    </Card>
  );
}

/** Current-user-only log browser. Detail endpoints repeat the backend ownership check. */
export function MyLogsPanel() {
  const [kind, setKind] = useState<LogKind>('tools');
  const [rows, setRows] = useState<LogRow[]>([]);
  const [summary, setSummary] = useState<MyUsageSummaryItem[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [detailKind, setDetailKind] = useState<DetailKind | null>(null);
  const [detail, setDetail] = useState<LogDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const reload = useCallback(async (targetKind: LogKind, targetPage: number) => {
    setLoading(true);
    try {
      let response: MyLogPage<LogRow>;
      if (targetKind === 'tools') {
        response = await getMyToolLogs({ page: targetPage, pageSize: PAGE_SIZE });
      } else if (targetKind === 'skills') {
        response = await getMySkillLogs({ page: targetPage, pageSize: PAGE_SIZE });
      } else if (targetKind === 'subagents') {
        response = await getMySubagentLogs({ page: targetPage, pageSize: PAGE_SIZE });
      } else {
        const [usage, usageSummary] = await Promise.all([
          getMyUsage({ page: targetPage, pageSize: PAGE_SIZE }),
          getMyUsageSummary('model'),
        ]);
        setSummary(usageSummary);
        response = usage;
      }
      setRows(response.items);
      setTotal(response.pagination.total_items);
    } catch (error) {
      message.error(t('加载日志失败：{msg}', { msg: (error as Error).message }));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void reload(kind, page); }, [kind, page, reload]);

  const openDetail = async (targetKind: DetailKind, logId: string) => {
    setDetailKind(targetKind);
    setDetail(null);
    setDetailLoading(true);
    try {
      if (targetKind === 'tools') {
        setDetail({ kind: targetKind, data: await getMyToolLog(logId) });
      } else if (targetKind === 'skills') {
        setDetail({ kind: targetKind, data: await getMySkillLog(logId) });
      } else {
        setDetail({ kind: targetKind, data: await getMySubagentLog(logId) });
      }
    } catch (error) {
      message.error(t('加载日志详情失败：{msg}', { msg: (error as Error).message }));
      setDetailKind(null);
    } finally {
      setDetailLoading(false);
    }
  };

  const commonColumns: ColumnsType<LogRow> = [
    {
      title: t('时间'), dataIndex: 'created_at', width: 165,
      render: (value: string | null) => <Text style={{ fontSize: 12 }}>{fmtTime(value)}</Text>,
    },
    {
      title: t('会话'), dataIndex: 'session_title', ellipsis: true,
      render: (value: string | null) => value || '—',
    },
  ];

  const detailAction = (targetKind: DetailKind) => ({
    title: t('操作'),
    key: 'action',
    width: 72,
    fixed: 'right' as const,
    render: (_value: unknown, record: LogRow) => (
      'id' in record && (
        <Button type="link" size="small" onClick={() => void openDetail(targetKind, record.id)}>
          {t('详情')}
        </Button>
      )
    ),
  });

  const columnsByKind: Record<LogKind, ColumnsType<LogRow>> = {
    tools: [
      ...commonColumns,
      {
        title: t('工具'), dataIndex: 'tool_name',
        render: (value: string, record: LogRow) => (
          'tool_display_name' in record ? record.tool_display_name || value : value
        ),
      },
      { title: t('来源'), dataIndex: 'source', width: 100 },
      { title: t('耗时'), dataIndex: 'duration_ms', width: 90, render: duration },
      { title: t('状态'), dataIndex: 'status', width: 90, render: statusTag },
      detailAction('tools'),
    ],
    skills: [
      ...commonColumns,
      {
        title: t('技能'), dataIndex: 'skill_name',
        render: (value: string | null, record: LogRow) => (
          value || ('skill_id' in record ? record.skill_id : '—')
        ),
      },
      {
        title: t('调用类型'), dataIndex: 'invocation_type', width: 110,
        render: (value: string | null) => value || '—',
      },
      { title: t('耗时'), dataIndex: 'duration_ms', width: 90, render: duration },
      { title: t('状态'), dataIndex: 'status', width: 90, render: statusTag },
      detailAction('skills'),
    ],
    subagents: [
      ...commonColumns,
      { title: t('子智能体'), dataIndex: 'subagent_name' },
      { title: t('耗时'), dataIndex: 'duration_ms', width: 90, render: duration },
      { title: t('状态'), dataIndex: 'status', width: 90, render: statusTag },
      detailAction('subagents'),
    ],
    usage: [
      ...commonColumns,
      { title: t('模型'), dataIndex: 'model', render: (value: string | null) => value || '—' },
      { title: t('输入 Token'), dataIndex: 'prompt_tokens', width: 110 },
      { title: t('输出 Token'), dataIndex: 'completion_tokens', width: 110 },
      { title: t('合计'), dataIndex: 'total_tokens', width: 90 },
    ],
  };

  const renderToolDetail = (item: MyToolLogItem) => (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Descriptions size="small" column={2} bordered>
        <Descriptions.Item label={t('工具名')}>{item.tool_name}</Descriptions.Item>
        <Descriptions.Item label="MCP Server">{item.mcp_server || '—'}</Descriptions.Item>
        <Descriptions.Item label={t('状态')}>{statusTag(item.status)}</Descriptions.Item>
        <Descriptions.Item label={t('耗时')}>{duration(item.duration_ms)}</Descriptions.Item>
        <Descriptions.Item label={t('来源')}>
          <Tag color={SOURCE_COLORS[item.source] || 'default'}>{item.source}</Tag>
        </Descriptions.Item>
        <Descriptions.Item label={t('调用时间')}>{fmtTime(item.created_at)}</Descriptions.Item>
        <Descriptions.Item label={t('会话')}>{item.session_title || '—'}</Descriptions.Item>
        <Descriptions.Item label="Sandbox">{item.sandbox_id || '—'}</Descriptions.Item>
        <Descriptions.Item label="trace_id" span={2}>
          <Text code copyable>{item.trace_id || '—'}</Text>
        </Descriptions.Item>
        {item.error_message && (
          <Descriptions.Item label={t('错误信息')} span={2}>
            <Text type="danger">{item.error_message}</Text>
          </Descriptions.Item>
        )}
      </Descriptions>
      <DetailCard title={t('入参')} value={item.tool_args} />
      <DetailCard
        title={`${t('输出')}${item.result_truncated ? ` (${t('已截断')})` : ''}`}
        value={item.tool_result}
        maxHeight={400}
      />
    </Space>
  );

  const renderSkillDetail = (item: MySkillLogItem) => (
    <Tabs items={[
      {
        key: 'overview',
        label: t('概览'),
        children: (
          <Descriptions size="small" column={2} bordered>
            <Descriptions.Item label={t('技能 ID')}>{item.skill_id}</Descriptions.Item>
            <Descriptions.Item label={t('技能名')}>{item.skill_name || '—'}</Descriptions.Item>
            <Descriptions.Item label={t('版本')}>{item.skill_version || '—'}</Descriptions.Item>
            <Descriptions.Item label={t('来源')}>{item.skill_source || '—'}</Descriptions.Item>
            <Descriptions.Item label={t('调用方式')}>
              <Tag color={INVOCATION_COLORS[item.invocation_type || ''] || 'default'}>
                {item.invocation_type || '—'}
              </Tag>
            </Descriptions.Item>
            <Descriptions.Item label={t('状态')}>{statusTag(item.status)}</Descriptions.Item>
            <Descriptions.Item label={t('脚本')}>{item.script_name || '—'}</Descriptions.Item>
            <Descriptions.Item label={t('语言')}>{item.script_language || '—'}</Descriptions.Item>
            <Descriptions.Item label={t('耗时')}>{duration(item.duration_ms)}</Descriptions.Item>
            <Descriptions.Item label={t('退出码')}>{item.exit_code ?? '—'}</Descriptions.Item>
            <Descriptions.Item label={t('会话')}>{item.session_title || '—'}</Descriptions.Item>
            <Descriptions.Item label="trace_id">
              <Text code copyable>{item.trace_id || '—'}</Text>
            </Descriptions.Item>
            {item.error_message && (
              <Descriptions.Item label={t('错误')} span={2}>
                <Text type="danger">{item.error_message}</Text>
              </Descriptions.Item>
            )}
          </Descriptions>
        ),
      },
      {
        key: 'io',
        label: t('入参 / 输出'),
        children: (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <DetailCard title="script_args" value={item.script_args} maxHeight={200} />
            {item.script_stdin && <DetailCard title="stdin" value={item.script_stdin} />}
            <DetailCard
              title={`stdout${item.output_truncated ? ` (${t('已截断')})` : ''}`}
              value={item.script_stdout}
            />
            <DetailCard title="stderr" value={item.script_stderr} maxHeight={200} />
          </Space>
        ),
      },
    ]} />
  );

  const renderSubagentDetail = (item: MySubagentLogDetail) => (
    <Tabs items={[
      {
        key: 'overview',
        label: t('概览'),
        children: (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Descriptions size="small" column={2} bordered>
              <Descriptions.Item label={t('名称')}>{item.subagent_name}</Descriptions.Item>
              <Descriptions.Item label={t('类型')}>{item.subagent_type || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('状态')}>{statusTag(item.status)}</Descriptions.Item>
              <Descriptions.Item label={t('耗时')}>{duration(item.duration_ms)}</Descriptions.Item>
              <Descriptions.Item label={t('模型')}>{item.model || '—'}</Descriptions.Item>
              <Descriptions.Item label="plan_id">{item.plan_id || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('工具调用数')}>{item.tool_calls_count ?? 0}</Descriptions.Item>
              <Descriptions.Item label={t('技能调用数')}>{item.skill_calls_count ?? 0}</Descriptions.Item>
              <Descriptions.Item label={t('会话')}>{item.session_title || '—'}</Descriptions.Item>
              <Descriptions.Item label="trace_id">
                <Text code copyable>{item.trace_id || '—'}</Text>
              </Descriptions.Item>
              {item.token_usage && (
                <Descriptions.Item label="Token" span={2}>
                  prompt: {item.token_usage.prompt_tokens || 0}; completion:{' '}
                  {item.token_usage.completion_tokens || 0}; total:{' '}
                  {item.token_usage.total_tokens || 0}; LLM calls:{' '}
                  {item.token_usage.llm_call_count || 0}
                </Descriptions.Item>
              )}
              {item.error_message && (
                <Descriptions.Item label={t('错误')} span={2}>
                  <Text type="danger">{item.error_message}</Text>
                </Descriptions.Item>
              )}
            </Descriptions>
            <DetailCard title={t('输入')} value={item.input_messages} maxHeight={200} />
            <DetailCard title={t('输出')} value={item.output_content} maxHeight={400} />
          </Space>
        ),
      },
      {
        key: 'steps',
        label: t('子步骤 ({n})', { n: item.child_steps.length }),
        children: item.child_steps.length === 0 ? <Text type="secondary">—</Text> : (
          <Timeline items={item.child_steps.map((stepItem) => ({
            color: stepItem.status === 'success' ? 'green' : stepItem.status === 'failed' ? 'red' : 'blue',
            children: (
              <Space direction="vertical" size={4} style={{ width: '100%' }}>
                <Text strong>
                  {stepItem.step_index != null ? t('步骤 {n}：', { n: stepItem.step_index }) : ''}
                  {stepItem.step_title || stepItem.subagent_name}
                </Text>
                <Space size={8} wrap>
                  {statusTag(stepItem.status)}
                  <Tag>{duration(stepItem.duration_ms)}</Tag>
                  <Tag color="blue">{stepItem.tool_calls_count ?? 0} {t('工具')}</Tag>
                </Space>
                {stepItem.output_content && (
                  <Paragraph style={{ margin: 0, whiteSpace: 'pre-wrap', color: '#555' }}>
                    {stepItem.output_content.slice(0, 400)}
                    {stepItem.output_content.length > 400 ? '…' : ''}
                  </Paragraph>
                )}
              </Space>
            ),
          }))} />
        ),
      },
      {
        key: 'tools',
        label: t('内部工具调用 ({n})', { n: item.tool_calls.length }),
        children: item.tool_calls.length === 0 ? <Text type="secondary">—</Text> : (
          <Table<MyToolLogItem>
            size="small"
            dataSource={item.tool_calls}
            rowKey="id"
            pagination={false}
            columns={[
              { title: t('时间'), dataIndex: 'created_at', width: 170, render: fmtTime },
              { title: t('工具'), dataIndex: 'tool_name' },
              { title: t('状态'), dataIndex: 'status', width: 80, render: statusTag },
              { title: t('耗时'), dataIndex: 'duration_ms', width: 90, render: duration },
            ]}
            expandable={{
              expandedRowRender: (tool) => (
                <pre style={{ maxHeight: 220, overflow: 'auto', margin: 0, fontSize: 12 }}>
                  {jsonText({ args: tool.tool_args, result: tool.tool_result })}
                </pre>
              ),
            }}
          />
        ),
      },
      {
        key: 'skills',
        label: t('内部技能调用 ({n})', { n: item.skill_calls.length }),
        children: item.skill_calls.length === 0 ? <Text type="secondary">—</Text> : (
          <Table<MySkillLogItem>
            size="small"
            dataSource={item.skill_calls}
            rowKey="id"
            pagination={false}
            columns={[
              { title: t('时间'), dataIndex: 'created_at', width: 170, render: fmtTime },
              { title: t('技能'), dataIndex: 'skill_name' },
              { title: t('脚本'), dataIndex: 'script_name' },
              { title: t('方式'), dataIndex: 'invocation_type', width: 100 },
              { title: t('状态'), dataIndex: 'status', width: 80, render: statusTag },
            ]}
          />
        ),
      },
    ]} />
  );

  const drawerTitle = detail
    ? detail.kind === 'tools'
      ? `${detail.data.tool_display_name || detail.data.tool_name} · ${t('调用详情')}`
      : detail.kind === 'skills'
        ? `${detail.data.skill_name || detail.data.skill_id} · ${t('调用详情')}`
        : t('{name} · 详情', { name: detail.data.subagent_name })
    : t('调用详情');

  return (
    <div className="jx-sysPanel">
      <div className="jx-sysPanel-toolbar">
        <Segmented
          value={kind}
          onChange={(value) => {
            setKind(value as LogKind);
            setPage(1);
            setDetailKind(null);
            setDetail(null);
          }}
          options={[
            { value: 'tools', label: t('工具调用') },
            { value: 'skills', label: t('技能调用') },
            { value: 'subagents', label: t('子智能体') },
            { value: 'usage', label: t('模型用量') },
          ]}
        />
      </div>
      {kind === 'usage' && summary.length > 0 && (
        <div className="jx-sysPanel-usageSummary">
          {summary.map((item) => (
            <Tag key={item.group_key}>
              {item.group_key}: {item.total_tokens.toLocaleString()} tokens /{' '}
              {item.total_requests} {t('次')}
            </Tag>
          ))}
        </div>
      )}
      <Table<LogRow>
        size="small"
        rowKey={(record) => ('id' in record ? record.id : record.message_id)}
        loading={loading}
        dataSource={rows}
        columns={columnsByKind[kind]}
        scroll={{ x: kind === 'usage' ? 850 : 980 }}
        pagination={{
          current: page,
          pageSize: PAGE_SIZE,
          total,
          showSizeChanger: false,
          showTotal: (count) => t('共 {n} 条', { n: count }),
          onChange: setPage,
        }}
      />

      <Drawer
        title={drawerTitle}
        width={detailKind === 'subagents'
          ? 'min(900px, calc(100vw - 24px))'
          : 'min(820px, calc(100vw - 24px))'}
        open={detailKind !== null}
        loading={detailLoading}
        onClose={() => {
          setDetailKind(null);
          setDetail(null);
        }}
      >
        {detail?.kind === 'tools' && renderToolDetail(detail.data)}
        {detail?.kind === 'skills' && renderSkillDetail(detail.data)}
        {detail?.kind === 'subagents' && renderSubagentDetail(detail.data)}
      </Drawer>
    </div>
  );
}
