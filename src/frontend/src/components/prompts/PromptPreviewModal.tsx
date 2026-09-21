import { Alert, Collapse, Modal, Space, Tabs, Tag, Typography } from 'antd';
import { t } from '../../i18n';

export interface PromptPreview {
  version_id: string;
  prompt: string;
  bash_tool: string;
  conditional_parts: Record<string, string>;
  environment_source: string;
  preview_mode: string;
  draft?: boolean;
}

const text = (value: string) => <Typography.Paragraph copyable={{ text: value }} style={{ whiteSpace: 'pre-wrap', fontFamily: 'monospace', overflowWrap: 'anywhere' }}>{value}</Typography.Paragraph>;

export function PromptPreviewModal({ data, onClose }: { data: PromptPreview | null; onClose: () => void }) {
  return <Modal title={t('桌面端完整提示词预览（默认模式，未绑定项目）')} open={!!data} onCancel={onClose} footer={null} width="min(1000px, 95vw)">
    {data && <Space direction="vertical" style={{ width: '100%' }}>
      <Space wrap><Tag>{data.version_id}</Tag><Tag>{data.draft ? t('未保存的编辑') : t('已保存版本')}</Tag><Tag>{data.prompt.length.toLocaleString()} {t('字符')}</Tag></Space>
      <Alert showIcon type="info" message={data.environment_source === 'local_host'
        ? t('环境字段来自当前桌面后端；预览使用独立会话与 ask 权限档。')
        : t('云端无法读取用户电脑环境；以下运行时占位符由桌面端实际系统填写。')}
        description={data.preview_mode === 'runtime' ? t('已包含当前后端装配的工具与运行时提示词。') : t('模板预览包含全部桌面片段；会话专属工具、技能、MCP 和动态策略未装配。')} />
      <Tabs items={[
        { key: 'prompt', label: t('系统提示词'), children: text(data.prompt) },
        { key: 'tool', label: t('工具说明'), children: text(data.bash_tool) },
        { key: 'conditional', label: t('条件片段'), children: <><Typography.Paragraph type="secondary">{t('以下为按场景注入的条件片段，不会全部同时发送：')}</Typography.Paragraph><Collapse items={Object.entries(data.conditional_parts).map(([key, value]) => ({ key, label: key, children: text(value) }))} /></> },
      ]} style={{ maxHeight: '65vh', overflow: 'auto', width: '100%' }} />
    </Space>}
  </Modal>;
}
