import { useCallback, useEffect, useState } from 'react';
import { Segmented, Table, Tag, Typography, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  getMySkillLogs,
  getMySubagentLogs,
  getMyToolLogs,
  getMyUsage,
  getMyUsageSummary,
  type MyLogPage,
  type MySkillLogItem,
  type MySubagentLogItem,
  type MyToolLogItem,
  type MyUsageItem,
  type MyUsageSummaryItem,
} from '../../api';
import { t } from '../../i18n';

const { Text } = Typography;

type LogKind = 'tools' | 'skills' | 'subagents' | 'usage';

const PAGE_SIZE = 20;

function fmtTime(s?: string | null): string {
  if (!s) return '—';
  try {
    return new Date(s).toLocaleString('zh-CN', { hour12: false });
  } catch {
    return s;
  }
}

function statusTag(status: string) {
  const color = status === 'success' ? 'success' : status === 'running' ? 'processing' : 'error';
  return <Tag color={color}>{status}</Tag>;
}

/**
 * "Settings → System Management → My Logs" panel: shows the current user's own
 * tool / skill / sub-agent invocation logs and model usage (/v1/me/logs; the
 * backend forces user_id = the current user).
 */
export function MyLogsPanel() {
  const [kind, setKind] = useState<LogKind>('tools');
  const [rows, setRows] = useState<Array<Record<string, unknown>>>([]);
  const [summary, setSummary] = useState<MyUsageSummaryItem[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);

  const reload = useCallback(async (k: LogKind, p: number) => {
    setLoading(true);
    try {
      let resp: MyLogPage<MyToolLogItem | MySkillLogItem | MySubagentLogItem | MyUsageItem>;
      if (k === 'tools') resp = await getMyToolLogs({ page: p, pageSize: PAGE_SIZE });
      else if (k === 'skills') resp = await getMySkillLogs({ page: p, pageSize: PAGE_SIZE });
      else if (k === 'subagents') resp = await getMySubagentLogs({ page: p, pageSize: PAGE_SIZE });
      else {
        const [usage, sum] = await Promise.all([
          getMyUsage({ page: p, pageSize: PAGE_SIZE }),
          getMyUsageSummary('model'),
        ]);
        setSummary(sum);
        resp = usage;
      }
      setRows(resp.items as Array<Record<string, unknown>>);
      setTotal(resp.pagination.total_items);
    } catch (e) {
      message.error(t('加载日志失败：{msg}', { msg: (e as Error).message }));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void reload(kind, page); }, [kind, page, reload]);

  const commonCols: ColumnsType<Record<string, unknown>> = [
    {
      title: t('时间'),
      dataIndex: 'created_at',
      width: 165,
      render: (v: string | null) => <Text style={{ fontSize: 12 }}>{fmtTime(v)}</Text>,
    },
    {
      title: t('会话'),
      dataIndex: 'session_title',
      ellipsis: true,
      render: (v: string | null) => v || '—',
    },
  ];

  const columnsByKind: Record<LogKind, ColumnsType<Record<string, unknown>>> = {
    tools: [
      ...commonCols,
      {
        title: t('工具'),
        dataIndex: 'tool_name',
        render: (v: string, r) => (r.tool_display_name as string) || v,
      },
      { title: t('来源'), dataIndex: 'source', width: 100 },
      { title: t('耗时'), dataIndex: 'duration_ms', width: 90, render: (v: number | null) => (v != null ? `${v}ms` : '—') },
      { title: t('状态'), dataIndex: 'status', width: 90, render: (v: string) => statusTag(v) },
    ],
    skills: [
      ...commonCols,
      { title: t('技能'), dataIndex: 'skill_name' },
      { title: t('调用类型'), dataIndex: 'invocation_type', width: 110, render: (v: string | null) => v || '—' },
      { title: t('耗时'), dataIndex: 'duration_ms', width: 90, render: (v: number | null) => (v != null ? `${v}ms` : '—') },
      { title: t('状态'), dataIndex: 'status', width: 90, render: (v: string) => statusTag(v) },
    ],
    subagents: [
      ...commonCols,
      { title: t('子智能体'), dataIndex: 'subagent_name' },
      { title: t('耗时'), dataIndex: 'duration_ms', width: 90, render: (v: number | null) => (v != null ? `${v}ms` : '—') },
      { title: t('状态'), dataIndex: 'status', width: 90, render: (v: string) => statusTag(v) },
    ],
    usage: [
      ...commonCols,
      { title: t('模型'), dataIndex: 'model', render: (v: string | null) => v || '—' },
      { title: t('输入 Token'), dataIndex: 'prompt_tokens', width: 110 },
      { title: t('输出 Token'), dataIndex: 'completion_tokens', width: 110 },
      { title: t('合计'), dataIndex: 'total_tokens', width: 90 },
    ],
  };

  return (
    <div className="jx-sysPanel">
      <div className="jx-sysPanel-toolbar">
        <Segmented
          value={kind}
          onChange={(v) => { setKind(v as LogKind); setPage(1); }}
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
          {summary.map((s) => (
            <Tag key={s.group_key}>
              {s.group_key}: {s.total_tokens.toLocaleString()} tokens / {s.total_requests} {t('次')}
            </Tag>
          ))}
        </div>
      )}
      <Table<Record<string, unknown>>
        size="small"
        rowKey={(r) => (r.id as string) || (r.message_id as string)}
        loading={loading}
        dataSource={rows}
        columns={columnsByKind[kind]}
        pagination={{
          current: page,
          pageSize: PAGE_SIZE,
          total,
          showSizeChanger: false,
          onChange: (p) => setPage(p),
        }}
      />
    </div>
  );
}
