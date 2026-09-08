import type { DeviceCapabilityItem, DeviceCapabilityKind } from '../../api';
import { t } from '../../i18n';
import { DEVICE_SOURCE_LABEL } from '../../utils/deviceCapabilities';

export function DeviceCapabilityChoice({ kind, runtimeName, candidates, preference, onChoose, disabled = false }: {
  kind: DeviceCapabilityKind;
  runtimeName: string;
  candidates: DeviceCapabilityItem[];
  preference?: string;
  onChoose: (id: string | null) => void;
  disabled?: boolean;
}) {
  if (kind === 'plugin' || (candidates.length < 2 && !preference)) return null;
  return (
    <select className="jx-devcap-choice" aria-label={t('选择本机使用的同名能力')}
      value={preference || ''} disabled={disabled} onClick={(event) => event.stopPropagation()}
      onChange={(event) => { event.stopPropagation(); onChoose(event.target.value || null); }}>
      <option value="">{t('自动选择')}</option>
      {candidates.map((candidate) => (
        <option key={candidate.install_id} value={candidate.install_id} disabled={!candidate.usable}>
          {t(DEVICE_SOURCE_LABEL[candidate.source] || candidate.source)} · {candidate.display_name || runtimeName}
          {candidate.revision ? ' · ' + candidate.revision.slice(0, 12) : ''}
          {candidate.profile ? ' · ' + candidate.profile : ''}
        </option>
      ))}
    </select>
  );
}
