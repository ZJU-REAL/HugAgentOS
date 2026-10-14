import { useEffect, useState } from 'react';
import { Alert, Button, Radio, Spin } from 'antd';
import type { EnvironmentField, ProjectEnvironment } from '../../api/projectEnvironment';
import ProjectEnvironmentMarkets from './ProjectEnvironmentMarkets';
import ProjectEnvironmentResourcePicker from './ProjectEnvironmentResourcePicker';
import { visibleEnvironmentDependencies } from './environmentDependencyDisplay';
import { useProjectEnvironment } from './useProjectEnvironment';
import { t } from '../../i18n';

const fields: EnvironmentField[] = ['agent_ids', 'plugin_ids', 'skill_ids', 'mcp_ids'];

export default function ProjectEnvironmentEditor({ value, onChange, projectId, onReadyChange }: {
  value: ProjectEnvironment;
  onChange: (value: ProjectEnvironment) => void;
  projectId?: string;
  onReadyChange?: (ready: boolean) => void;
}) {
  const [market, setMarket] = useState<EnvironmentField | null>(null);
  const { options, loading, error, preview, previewError, ready, load, installed, retryPreview } =
    useProjectEnvironment(value, onChange, projectId);

  const dependencies = visibleEnvironmentDependencies(value, options, preview);

  useEffect(() => { onReadyChange?.(ready); }, [ready, onReadyChange]);

  return <div className="jx-projectEnvironment">
    <Radio.Group className="jx-projectEnvironment-modes" value={value.mode}
      onChange={event => onChange({ ...value, mode: event.target.value })}>
      <Radio value="all">
        <strong>{t('全部可用环境')}</strong>
        <span>{t('使用当前账号可用的资源，自动跟随新增资源')}</span>
      </Radio>
      <Radio value="custom">
        <strong>{t('自定义项目环境')}</strong>
        <span>{t('仅注册和调用此项目绑定的资源及其依赖')}</span>
      </Radio>
    </Radio.Group>

    {value.mode === 'all' ? <div className="jx-projectEnvironment-hint">
      {t('主智能体保持现状。子智能体、插件、技能和连接器沿用当前账号的可用环境。')}
    </div> : <>
      {error && <Alert type="error" message={error}
        action={<Button onClick={() => void load()}>{t('重试')}</Button>} />}
      <Spin spinning={loading}>
        {fields.map(field => <ProjectEnvironmentResourcePicker key={field}
          field={field} value={value[field]} options={options?.[field]}
          dependencies={dependencies}
          onChange={ids => onChange({ ...value, [field]: ids })}
          onMarket={() => setMarket(field)} />)}
      </Spin>
      {previewError && <Alert type="error" message={previewError}
        action={<Button onClick={retryPreview}>{t('重试')}</Button>} />}
      {!!preview?.unavailable.length && <Alert type="warning"
        message={t('部分依赖暂不可用，绑定不会授予额外权限。')}
        description={preview.unavailable.map(item => item.id).join('、')} />}
    </>}
    <ProjectEnvironmentMarkets kind={market} onClose={() => setMarket(null)}
      onInstalled={(field, id) => { void installed(field, id); }} />
  </div>;
}
