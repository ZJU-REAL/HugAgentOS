import { Modal } from 'antd';
import type { ReactNode } from 'react';
import { useDelayedFlag } from '../hooks/useDelayedFlag';
import { t } from '../i18n';
import { useAuthStore } from '../stores/authStore';
import { useDeploymentModeStore } from '../stores/deploymentModeStore';
import { AppLoadingSkeleton, AuthExpiredModal } from './common';
import { CapabilitySyncGate } from './desktop/CapabilitySyncGate';
import { FirstRunSetup } from './onboarding';
import { PasswordManagementPanel } from './settings';
export function AppAuthGate({ children }: { children: ReactNode }) {
  const { authUser, authChecking, authExpiredUrl, setAuthUser } = useAuthStore();
  const { isDesktop: isDesktopShell, provisionMode: desktopProvisionMode, capabilityGateOpen, capabilitiesReady, loaded: deploymentModeLoaded } = useDeploymentModeStore();
  const showAuthSkeleton = useDelayedFlag(authChecking);

  if (authChecking) {
    return showAuthSkeleton ? <AppLoadingSkeleton /> : null;
  }

  if (!authUser || window.location.pathname.startsWith('/mock-sso/login')) {
    return authExpiredUrl ? <AuthExpiredModal /> : null;
  }

  if (authUser.must_change_password) {
    return (
      <Modal
        open
        title={t('修改默认密码')}
        footer={null}
        closable={false}
        maskClosable={false}
        keyboard={false}
        width={480}
      >
        <PasswordManagementPanel forced />
      </Modal>
    );
  }

  // CE 首次配置向导（模型/搜索引擎等）只面向云端部署/Web 侧与桌面纯本机形态。
  // 桌面双模式跳过：配置以云端为准并经身份桥下发本机，客户端无需再引导一遍。
  if (authUser.onboarding_required) {
    if (!deploymentModeLoaded) {
      return null; // 部署形态探测完成前不闪现向导（web 上探测瞬时完成）
    }
    if (!(isDesktopShell && desktopProvisionMode === 'dual')) {
      return (
        <FirstRunSetup
          user={authUser}
          onComplete={() => setAuthUser({ ...authUser, onboarding_required: false })}
        />
      );
    }
  }

  if (desktopProvisionMode === 'dual' && (capabilityGateOpen || !capabilitiesReady)) {
    return <CapabilitySyncGate />;
  }

  return children;
}
