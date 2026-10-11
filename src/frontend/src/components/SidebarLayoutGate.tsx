import { Alert, Button } from 'antd';
import { useEffect, type ReactNode } from 'react';
import { t } from '../i18n';
import { useSidebarLayoutStore } from '../stores/sidebarLayoutStore';
import { AppLoadingSkeleton } from './common';

export function SidebarLayoutGate({ userId, children }: { userId: string; children: ReactNode }) {
  const { userId: loadedUser, status, load, reset } = useSidebarLayoutStore();
  useEffect(() => {
    void load(userId);
    return reset;
  }, [userId, load, reset]);
  if (loadedUser !== userId || status === 'idle' || status === 'loading') return <AppLoadingSkeleton />;
  if (status === 'error') return <Alert type="error" title={t('无法读取排布，请重试。')}
    action={<Button onClick={() => void load(userId)}>{t('重试')}</Button>} />;
  return children;
}
