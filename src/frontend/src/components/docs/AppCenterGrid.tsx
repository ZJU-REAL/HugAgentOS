import type { ApplicationEntry } from '../../applicationEntries';
import { t } from '../../i18n';
import { ApplicationIcon } from '../../workbench/ApplicationIcon';
import { ApplicationBadge, ApplicationActivityStatus } from '../../workbench/ApplicationBadge';
import { staggerStyle } from '../../utils/motionTokens';

export function AppCenterGrid({ entries, onOpen }: {
  entries: ApplicationEntry[]; onOpen: (entry: ApplicationEntry) => void;
}) {
  return <div className="jx-agentPage-grid jx-anim-stagger">
    {entries.map((entry, index) => <button key={entry.key} type="button" data-application-open={entry.key}
      className={'jx-agentCard jx-appCenterCard jx-card-lift' + (!entry.item.availability.available ? ' jx-agentCard--disabled' : '')}
      style={staggerStyle(index)} disabled={!entry.item.availability.available} onClick={() => onOpen(entry)}>
      <span className="jx-agentCard-body">
        <span className="jx-agentCard-head">
          <ApplicationIcon icon={entry.icon} builtinIcon={entry.builtinIcon} />
          <span className="jx-agentCard-nameRow"><span className="jx-agentCard-name">{entry.title}</span></span>
          <ApplicationBadge applicationKey={entry.key} />
          <ApplicationActivityStatus applicationKey={entry.key} />
        </span>
        {entry.description && <span className="jx-agentCard-desc">{entry.description}</span>}
        {entry.source && <span className="jx-appCenterCard-source">{entry.source}</span>}
        {!entry.item.availability.available && <span className="jx-appCenterCard-source">
          {t(entry.item.availability.reason === 'auth_configuration_required' ? '请管理员配置此应用的目标认证适配' : entry.item.availability.reason === 'app_disabled' ? '应用已停用' : '暂不可用')}
        </span>}
      </span>
    </button>)}
  </div>;
}
