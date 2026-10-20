import { usePanelHeader } from '../../hooks/usePageConfig';
import { useApplicationEntries } from '../../applicationEntries';
import { useApplicationOpen } from '../../applicationOpen';
import { AppCenterGrid } from './AppCenterGrid';
import { reloadDirectory } from '../../workbench/catalog';
import { t } from '../../i18n';

export default function AppCenterPanel() {
  const {catalog,entries} = useApplicationEntries();
  const {open,feedback} = useApplicationOpen();
  const {title,subtitle} = usePanelHeader('app_center', {title:'应用中心',subtitle:'基于 AI 能力的场景化智能应用'});
  return <div className="jx-agentPage">
    <div className="jx-agentPage-header"><div><div className="jx-agentPage-title">{title}</div>
      {subtitle && <div className="jx-agentPage-subtitle">{subtitle}</div>}</div></div>
    {catalog.loading && <p role="status">{t('正在加载应用目录…')}</p>}
    {catalog.error && <p role="alert">{t(catalog.error)} <button onClick={reloadDirectory}>{t('重试')}</button></p>}
    <p role="status">{feedback}</p>
    {!catalog.loading && !catalog.error && !entries.length && <p role="status">{t('暂无可用应用')}</p>}
    <AppCenterGrid entries={entries} onOpen={open} />
  </div>;
}
