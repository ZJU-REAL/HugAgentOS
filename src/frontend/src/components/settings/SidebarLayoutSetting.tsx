import { CheckOutlined } from '@ant-design/icons';
import { Button, Modal, message } from 'antd';
import { useState } from 'react';
import { useAuthStore } from '../../stores/authStore';
import { useSidebarLayoutStore } from '../../stores/sidebarLayoutStore';
import { t } from '../../i18n';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { applySidebarLayout, type SidebarLayout } from '../../utils/sidebarLayout';
import '../../styles/sidebar-layout-setting.css';

export function SidebarLayoutSetting() {
  const applied = useSidebarLayoutStore(s => s.layout);
  const userId = useAuthStore(s => s.authUser?.user_id);
  const [selected, setSelected] = useState(applied);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [applying, setApplying] = useState(false);
  const isDesktop = useDeploymentModeStore(s => s.isDesktop);
  const [messageApi, holder] = message.useMessage();
  const choices: { value: SidebarLayout; label: string; description: string }[] = [
    { value: 'classic', label: t('经典模式'), description: t('文字导航、项目与历史对话集中在同一侧栏') },
    { value: 'modern', label: t('现代模式'), description: t('图标导航与模块侧栏分开显示') },
  ];
  return <>
    {holder}
    <div className="jx-settings-card"><div className="jx-layoutSetting">
    <div className="jx-settings-rowLeft">
      <span className="jx-settings-rowLabel">{t('侧边栏排布')}</span>
      <span className="jx-settings-rowDesc">{t('选择排布后点击应用，确认后生效。')}</span>
    </div>
    <fieldset className="jx-layoutChoices">
      <legend className="jx-layoutLegend">{t('侧边栏排布')}</legend>
      {choices.map(choice => <label key={choice.value} className={`jx-layoutChoice${selected === choice.value ? ' selected' : ''}`}>
        <input type="radio" name="sidebar-layout" value={choice.value} checked={selected === choice.value}
          onChange={() => setSelected(choice.value)} disabled={applying} />
        <span className={`jx-layoutPreview jx-layoutPreview--${choice.value}`} aria-hidden="true">
          {choice.value === 'modern' && <span className="jx-layoutPreviewRail"><i /><i /><i /><i /></span>}
          <span className="jx-layoutPreviewSidebar"><b /><i /><i /><i /><i /><i /><i /></span>
          <span className="jx-layoutPreviewContent"><i /><i /><b /></span>
          {selected === choice.value && <span className="jx-layoutCheck"><CheckOutlined /></span>}
        </span>
        <span className="jx-layoutChoiceName">{choice.label}</span>
        <span className="jx-layoutChoiceDesc">{choice.description}</span>
      </label>)}
    </fieldset>
    </div></div>
    <div className="jx-layoutActions">
      <span className="jx-settings-rowDesc">{isDesktop ? t('应用后将重启客户端，所有窗口都会关闭。') : t('应用后将刷新当前页面。')}</span>
      <Button type="primary" disabled={selected === applied || applying} onClick={() => setConfirmOpen(true)}>{t('应用')}</Button>
    </div>
    <Modal open={confirmOpen} title={isDesktop ? t('重启客户端并应用排布？') : t('刷新页面并应用排布？')}
      okText={isDesktop ? t('应用并重启') : t('应用并刷新')} cancelText={t('取消')}
      confirmLoading={applying} closable={!applying} maskClosable={!applying}
      onCancel={() => { if (!applying) setConfirmOpen(false); }}
      onOk={async () => {
        if (!userId) return;
        setApplying(true);
        try {
          await applySidebarLayout(userId, selected);
          if (isDesktop) setConfirmOpen(false);
        } catch {
          messageApi.error(t('无法保存排布，请检查网络后重试。'));
        } finally {
          setApplying(false);
        }
      }}>
      <p>{isDesktop ? t('客户端将关闭所有窗口并重新打开。请先保存尚未完成的编辑。') : t('当前页面将重新加载。请先保存尚未完成的编辑。')}</p>
      <p>{isDesktop && t('重启会中断正在本机运行的任务。')}{t('取消将保留当前排布。')}</p>
    </Modal>
  </>;
}
