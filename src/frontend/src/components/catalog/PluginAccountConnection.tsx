import { useState } from 'react';
import { Alert, Segmented } from 'antd';
import type { RequestTarget } from '../../api';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { DingTalkConnect } from '../settings/DingTalkConnect';
import { LarkConnect } from '../settings/LarkConnect';
import { EmailConnect } from '../settings/EmailConnect';
import { YidaConnect } from '../settings/YidaConnect';
import { LarkAppInitCard } from './LarkAppInitCard';
import { t } from '../../i18n';

const panels = { dingtalk: DingTalkConnect, lark: LarkConnect, email: EmailConnect, yida: YidaConnect };

export function PluginAccountConnection({ connection }: { connection: string }) {
  const { isDesktop, provisionMode, localReady } = useDeploymentModeStore();
  const dual = isDesktop && provisionMode === 'dual';
  const localOnly = isDesktop && provisionMode === 'local_only';
  const [selected, setSelected] = useState<RequestTarget>('local');
  const target: RequestTarget = dual ? selected : localOnly ? 'local' : 'cloud';
  const Panel = panels[connection as keyof typeof panels];
  if (!Panel) return null;

  return <div style={{ marginTop: 12 }}>
    <h4 className="jx-sectionTitle">{t('账号连接')}</h4>
    {dual && <Segmented
      aria-label={t('账号连接位置')} value={target}
      onChange={(value) => setSelected(value as RequestTarget)}
      options={[{ label: t('本机'), value: 'local' }, { label: t('云端'), value: 'cloud' }]}
      style={{ marginBottom: 12 }}
    />}
    <div className="jx-settings-card">
      {dual && target === 'local' && !localReady
        ? <Alert type="info" showIcon message={t('本机服务尚未就绪，请稍候重试。')} />
        : <div key={target}>
          {connection === 'lark' && <LarkAppInitCard target={target} />}
          <Panel target={target} />
        </div>}
    </div>
  </div>;
}
