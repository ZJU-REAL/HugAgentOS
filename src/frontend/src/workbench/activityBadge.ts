import { useActivity } from './activityStore';
import { t } from '../i18n';

export function activityAccessibleName(title: string, count: number, background = false) {
  return title + (count > 0 ? ' · ' + count + ' ' + t('未读') : '') +
    (background ? ' · ' + t('后台运行中') : '');
}
export function useApplicationActivity(applicationKey: string, legacyCount = 0) {
  const projection = useActivity();
  return {count: Math.max(legacyCount, projection.badges[applicationKey]?.count || 0),
    background: projection.background.some(row => row.applicationKey === applicationKey)};
}
