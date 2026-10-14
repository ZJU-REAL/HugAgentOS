import { useState } from 'react';
import { Alert, Button, Input, Segmented, Tag, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { getApiUrl, isLocalProject } from '../../api';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { resolveAgentApiEndpoint } from '../../utils/agentApiExamples';
import { projectApiExample } from '../../utils/projectApiExamples';
import { CopyButton } from '../common/CopyButton';
import { t } from '../../i18n';

function newChatId() {
  return `project-api-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export default function ProjectApiGuide({ projectId }: { projectId: string }) {
  const [stream, setStream] = useState(false);
  const [chatId, setChatId] = useState(newChatId);
  const deployment = useDeploymentModeStore();
  const local = deployment.activeLocal
    || (deployment.provisionMode === 'dual' && isLocalProject(projectId));
  const endpoint = resolveAgentApiEndpoint({
    origin: window.location.origin, apiBase: getApiUrl(), isDesktop: deployment.isDesktop,
    local, serverBase: deployment.serverBase,
    localBase: deployment.localBase || (deployment.activeLocal ? deployment.serverBase : ''),
  });
  const validChatId = /^[A-Za-z0-9_-]{1,64}$/.test(chatId);
  const example = projectApiExample(endpoint, projectId, stream, chatId);
  return <div className="jx-projectApi-guide">
    <div className="jx-projectApi-endpoint">
      <Tag color="blue">POST</Tag>
      <Typography.Text code>{endpoint}</Typography.Text>
      <CopyButton text={endpoint} aria-label={t('复制接口地址')} disabled={!endpoint} />
    </div>
    {!endpoint ? <Alert type="error" title={t('尚未获得后端地址，请等待连接就绪后重试。')} /> : <>
      <div className="jx-projectApi-session">
        <Typography.Text type="secondary">chat_id</Typography.Text>
        <Input aria-label="chat_id" value={chatId} maxLength={64}
          status={validChatId ? undefined : 'error'} onChange={event => setChatId(event.target.value)} />
        <Button icon={<ReloadOutlined />} onClick={() => setChatId(newChatId())}>{t('新会话')}</Button>
      </div>
      <div className="jx-projectApi-codeToolbar">
        <Segmented aria-label={t('响应方式')} value={stream ? 'stream' : 'json'}
          onChange={value => setStream(value === 'stream')}
          options={[{ value: 'json', label: 'JSON' }, { value: 'stream', label: 'SSE' }]} />
        <CopyButton text={example} disabled={!validChatId}>{t('复制示例')}</CopyButton>
      </div>
      <pre className="jx-agentApi-code">{example}</pre>
    </>}
  </div>;
}
