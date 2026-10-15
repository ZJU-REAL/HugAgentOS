import { usePanelHeader } from '../../hooks/usePageConfig';
import { usePanel } from '../../routing/usePanel';
import { useApplicationEntries } from '../../applicationEntries';
import { useApplicationOpen } from '../../applicationOpen';
import { ApplicationList } from '../../workbench/ApplicationList';
import { reloadDirectory } from '../../workbench/catalog';
import { t } from '../../i18n';
import '../../workbench/navigation.css';

export default function AppCenterPanel() {
  const {catalog,entries,defaults} = useApplicationEntries();
  const {open,feedback} = useApplicationOpen();
  const panel = usePanel();
  const {title,subtitle} = usePanelHeader('app_center', {title:'应用中心',subtitle:'基于 AI 能力的场景化智能应用'});
  return <div className="jx-agentPage">
    <div className="jx-agentPage-header"><div><div className="jx-agentPage-title">{title}</div>
      {subtitle && <div className="jx-agentPage-subtitle">{subtitle}</div>}</div></div>
    {catalog.loading && <p role="status">{t('正在加载应用目录…')}</p>}
    {catalog.error && <p role="alert">{t(catalog.error)} <button onClick={reloadDirectory}>{t('重试')}</button></p>}
    <p role="status">{feedback}</p>
    {!catalog.loading && !catalog.error && !entries.some(entry => entry.item.declaration.target.kind === 'external') &&
      <p>{t('暂无可用外部应用')}</p>}
    <ApplicationList entries={entries.filter(entry => entry.item.declaration.navigation?.showInAppCenter !== false && (!entry.item.legacy || entry.item.enabled))}
      defaults={defaults} currentKey={'builtin:' + panel} onOpen={open} />
  </div>;
}
