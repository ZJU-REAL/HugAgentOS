import { DownloadOutlined, ExclamationCircleFilled, MessageOutlined } from '@ant-design/icons';
import { Dropdown, Modal, Tooltip, message, type MenuProps } from 'antd';
import { useState } from 'react';
import { getApiUrl } from '../../api';
import { CapabilitySyncEntry } from '../../desktop/CapabilitySyncEntry';
import { clientDownloadTarget, latestClientDownloadUrl } from '../../desktop/clientDownload';
import { DesktopUpdateEntry } from '../../desktop/DesktopUpdateEntry';
import { useDesktopUpdateStatus } from '../../desktop/useDesktopUpdateStatus';
import { HELP_DOCUMENTATION_URL, IS_COMMUNITY_EDITION_BUILD } from '../../edition';
import { FeedbackModal } from '../../feedbackEdition';
import { usePageConfig } from '../../hooks/usePageConfig';
import { t } from '../../i18n';
import { useAuthStore } from '../../stores';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { resolveAvatarUrl } from '../../utils/avatar';
import type { SidebarProps } from './sidebarTypes';
export function SidebarUtilities({ onSetPanel }: Pick<SidebarProps, 'onSetPanel'>) {
  const { authUser, doLogout, loggingOut } = useAuthStore();
  const [footerMenuOpen, setFooterMenuOpen] = useState(false);
  const [logoutConfirmOpen, setLogoutConfirmOpen] = useState(false);
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [clientDownloadBusy, setClientDownloadBusy] = useState(false);
  const cfgLogoutTitle = usePageConfig('texts.dialog_logout_confirm_title', '确认退出登录？');
  const cfgLogoutContent = usePageConfig('texts.dialog_logout_confirm_content', '退出登录不会丢失任何数据，你仍可以登录此账号。');
  const cfgLogoutOk = usePageConfig('texts.dialog_logout_confirm_ok', '退出登录');
  const footerSettingsMenu: MenuProps = {
    items: [
      { key: 'settings', label: t('设置'), onClick: () => onSetPanel('settings') },
      ...(!IS_COMMUNITY_EDITION_BUILD && (authUser?.can_system_config || authUser?.can_content_manage)
        ? [{ type: 'divider' as const }]
        : []),
      ...(!IS_COMMUNITY_EDITION_BUILD && authUser?.can_system_config ? [{
        key: 'system_config',
        label: t('系统配置'),
        icon: <img src="/home/settings.svg" alt="" style={{ width: 16, height: 16 }} />,
        onClick: () => { window.location.href = '/config'; },
      }] : []),
      ...(!IS_COMMUNITY_EDITION_BUILD && authUser?.can_content_manage ? [{
        key: 'content_manage',
        label: t('内容管理'),
        icon: <img src="/home/knowledge.svg" alt="" style={{ width: 16, height: 16 }} />,
        onClick: () => { window.location.href = '/admin'; },
      }] : []),
      { type: 'divider' as const },
      {
        key: 'logout',
        label: t('退出登录'),
        icon: <img src="/home/logout.svg" alt="" style={{ width: 16, height: 16 }} />,
        danger: true,
        onClick: () => {
          // Let the dropdown exit before presenting its modal. WebView2 can
          // otherwise composite both overlays in one frame and visibly flash.
          setFooterMenuOpen(false);
          window.setTimeout(() => setLogoutConfirmOpen(true), 100);
        },
      },
    ],
  };
  const desktopUpdateStatus = useDesktopUpdateStatus();
  const isDesktop = useDeploymentModeStore((s) => s.isDesktop);
  const downloadClient = async () => {
    const target = clientDownloadTarget(navigator.userAgent, navigator.platform, navigator.maxTouchPoints);
    if (!target) {
      message.info(t('无法识别当前系统，请使用 Windows、macOS 或 Linux 浏览器下载'));
      return;
    }
    setClientDownloadBusy(true);
    try {
      const url = await latestClientDownloadUrl(target, getApiUrl());
      if (!url) {
        message.info(t('当前系统暂无已发布的客户端'));
        return;
      }
      window.location.href = url;
    } catch {
      message.error(t('获取客户端下载地址失败，请稍后重试'));
    } finally {
      setClientDownloadBusy(false);
    }
  };
  const helpMenu: MenuProps = {
    items: [
      ...(!IS_COMMUNITY_EDITION_BUILD ? [{
        key: 'feedback',
        label: t('问题反馈'),
        icon: <MessageOutlined style={{ fontSize: 16 }} />,
        onClick: () => setFeedbackOpen(true),
      }] : []),
      ...(!IS_COMMUNITY_EDITION_BUILD ? [{
        key: 'docs',
        label: t('更新记录'),
        icon: <img src="/home/updates.svg" alt="" style={{ width: 16, height: 16 }} />,
        onClick: () => onSetPanel('docs'),
      }] : []),
      ...(!IS_COMMUNITY_EDITION_BUILD && !isDesktop ? [{
        key: 'download_client',
        label: t('下载客户端'),
        icon: <DownloadOutlined style={{ fontSize: 16 }} />,
        disabled: clientDownloadBusy,
        onClick: () => { void downloadClient(); },
      }] : []),
      {
        key: IS_COMMUNITY_EDITION_BUILD ? 'official_docs' : 'manual',
        label: t(IS_COMMUNITY_EDITION_BUILD ? '官方文档' : '操作手册'),
        icon: <img src="/home/knowledge.svg" alt="" style={{ width: 16, height: 16 }} />,
        onClick: () => window.open(HELP_DOCUMENTATION_URL, '_blank', 'noopener,noreferrer'),
      },
    ],
  };

  return <>
    <div className="jx-miniRailFooter">
      <CapabilitySyncEntry className="jx-miniRailBtn" />
      <DesktopUpdateEntry status={desktopUpdateStatus} className="jx-miniRailBtn">
        <Dropdown menu={helpMenu} trigger={['click']} placement="topRight" overlayClassName="jx-settingsMenu">
          <Tooltip title={t(IS_COMMUNITY_EDITION_BUILD ? '官方文档' : '帮助 / 更新记录')} placement="right">
            <button type="button" className="jx-miniRailBtn" aria-label={t(IS_COMMUNITY_EDITION_BUILD ? '官方文档' : '帮助')}>
              <img src="/home/help.svg" alt="" className="jx-miniRailIcon" style={{ opacity: 0.55 }} />
            </button>
          </Tooltip>
        </Dropdown>
      </DesktopUpdateEntry>
      <Dropdown open={footerMenuOpen} onOpenChange={setFooterMenuOpen} menu={footerSettingsMenu} trigger={['click']} placement="topLeft" overlayClassName="jx-settingsMenu">
        <Tooltip title={authUser?.nickname || authUser?.real_name || authUser?.username || t('用户')} placement="right">
          <button type="button" className="jx-miniRailBtn jx-miniRailAvatarBtn" aria-label={t('用户菜单')}>
            <img src={resolveAvatarUrl(authUser?.avatar_url)} alt="" className="jx-miniRailAvatar" />
          </button>
        </Tooltip>
      </Dropdown>
    </div>
    <Modal
      title={(
        <span>
          <ExclamationCircleFilled style={{ color: 'var(--color-warning)', marginRight: 8 }} />
          {cfgLogoutTitle}
        </span>
      )}
      open={logoutConfirmOpen}
      okText={cfgLogoutOk}
      cancelText={t('取消')}
      okButtonProps={{ danger: true }}
      confirmLoading={loggingOut}
      maskClosable={!loggingOut}
      closable={!loggingOut}
      onCancel={() => setLogoutConfirmOpen(false)}
      onOk={() => void doLogout()}
    >
      {cfgLogoutContent}
    </Modal>
    <FeedbackModal open={feedbackOpen} onClose={() => setFeedbackOpen(false)} />
  </>;
}
