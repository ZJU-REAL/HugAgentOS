import { useState } from 'react';
import { Alert, Segmented, Tabs, Typography } from 'antd';
import { capabilitiesRouteLocal, getApiUrl } from '../../api';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { agentApiExamples, resolveAgentApiEndpoint } from '../../utils/agentApiExamples';
import { CopyButton } from '../common/CopyButton';
import { t } from '../../i18n';

export function AgentApiGuide({ agentId }: { agentId: string }) {
  const [stream, setStream] = useState(true);
  const deployment = useDeploymentModeStore();
  const local = deployment.activeLocal || (deployment.provisionMode === 'dual' && capabilitiesRouteLocal());
  const endpoint = resolveAgentApiEndpoint({
    origin: window.location.origin, apiBase: getApiUrl(), isDesktop: deployment.isDesktop,
    local, serverBase: deployment.serverBase,
    localBase: deployment.localBase || (deployment.activeLocal ? deployment.serverBase : ''),
  });
  const examples = agentApiExamples(endpoint, stream, agentId);
  return <div className="jx-agentApi-section">
    {local && <Alert type="info" showIcon title={t('此地址指向本机后端，仅能从这台电脑访问。')} />}
    {!endpoint ? <Alert type="error" title={t('尚未获得后端地址，请等待连接就绪后重试。')} /> : <>
      <div className="jx-agentApi-endpoint"><Typography.Text code>POST {endpoint}</Typography.Text>
        <CopyButton text={endpoint} aria-label={t('复制接口地址')} /></div>
      <Typography.Paragraph type="secondary">
        {t('请求体必须传入当前智能体的 agent_id。这里创建的 Key 只能调用这个智能体；设置中的全局 Key 也可指定此 ID。chat_id 用于继续同一 API 会话。')}
      </Typography.Paragraph>
      <Segmented aria-label={t('响应方式')} value={stream ? 'stream' : 'json'}
        onChange={value => setStream(value === 'stream')}
        options={[{ value: 'stream', label: t('流式 · stream: true') }, { value: 'json', label: t('非流式 · stream: false') }]} />
      <Typography.Paragraph>
        {stream ? t('流式返回 text/event-stream，以 data: [DONE] 结束；Python 示例逐行展示 SSE。') :
          t('非流式执行完成后直接返回 ChatResponse JSON；回复文本在 response 字段，同时包含 chat_id、sources、artifacts 等字段。')}
      </Typography.Paragraph>
      <Tabs items={(['curl', 'python'] as const).map(language => ({
        key: language, label: language === 'curl' ? 'cURL' : 'Python',
        children: <div>
          <CopyButton text={examples[language]}>{t('复制示例')}</CopyButton>
          <pre className="jx-agentApi-code">{examples[language]}</pre>
        </div>,
      }))} />
      <Typography.Paragraph type="secondary">{t('Python 示例从 AGENT_API_KEY 环境变量读取密钥。请勿将真实密钥写进源码或公开日志。')}</Typography.Paragraph>
    </>}
  </div>;
}
