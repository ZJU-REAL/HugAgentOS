import { Fragment, useEffect, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { AppstoreOutlined, BulbOutlined, PartitionOutlined } from '@ant-design/icons';
import { usePopupFlip } from '../../hooks/usePopupFlip';
import { t } from '../../i18n';
import type { InstalledPluginItem } from '../../types';

export type SlashEntry = {
  /** mode：切换对话模式的命令（如 /workflow 进入工作流模式），选中即开启，不插入 chip。 */
  kind: 'skill' | 'plugin' | 'mode';
  id: string;
  name: string;
  plugin?: InstalledPluginItem;
  /** 候选项的简短说明，和名称并排展示。 */
  description?: string;
};

interface SkillSlashPopupProps {
  entries: SlashEntry[];
  visible: boolean;
  selectedIndex: number;
  onSelect: (entry: SlashEntry) => void;
  onHover: (index: number) => void;
}

const POPUP_MAX_HEIGHT = 320;

function sectionLabel(kind: SlashEntry['kind']): string {
  if (kind === 'mode') return t('模式');
  if (kind === 'plugin') return t('插件');
  return t('技能');
}

export function SkillSlashPopup({ entries, visible, selectedIndex, onSelect, onHover }: SkillSlashPopupProps) {
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const popupRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (visible && itemRefs.current[selectedIndex]) {
      itemRefs.current[selectedIndex]!.scrollIntoView({ block: 'nearest' });
    }
  }, [selectedIndex, visible]);

  const showPopup = visible && entries.length > 0;
  // Not enough space above (e.g. the project detail page input box is near the top of the page) -> flip to below the cursor's line
  const { below: flipBelow, belowTop } = usePopupFlip(popupRef, showPopup, POPUP_MAX_HEIGHT);

  return (
    <AnimatePresence>
      {showPopup && (
        <motion.div
          ref={popupRef}
          className={`jx-slashPopup${flipBelow ? ' jx-slashPopup--below' : ''}`}
          style={flipBelow && belowTop != null ? { top: belowTop } : undefined}
          onMouseDown={(e) => e.preventDefault()}
          role="listbox"
          aria-label={t('斜杠命令建议')}
          initial={{ opacity: 0, y: flipBelow ? -6 : 6, scale: 0.97 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: flipBelow ? -4 : 4, scale: 0.97 }}
          transition={{ duration: 0.16, ease: 'easeOut' }}
        >
          {entries.map((entry, idx) => (
            <Fragment key={`${entry.kind}-${entry.id}`}>
              {(idx === 0 || entries[idx - 1]?.kind !== entry.kind) && (
                <div className="jx-commandPopup-groupTitle" role="presentation">
                  {sectionLabel(entry.kind)}
                </div>
              )}
              <button
                ref={(el) => { itemRefs.current[idx] = el; }}
                type="button"
                role="option"
                aria-selected={idx === selectedIndex}
                className={`jx-slashPopup-item${idx === selectedIndex ? ' active' : ''}`}
                onMouseEnter={() => onHover(idx)}
                onClick={() => onSelect(entry)}
              >
                {entry.kind === 'plugin'
                  ? <AppstoreOutlined className="jx-slashPopup-icon jx-slashPopup-icon--plugin" />
                  : entry.kind === 'mode'
                    ? <PartitionOutlined className="jx-slashPopup-icon jx-slashPopup-icon--mode" />
                    : <BulbOutlined className="jx-slashPopup-icon jx-slashPopup-icon--skill" />}
                <span className="jx-slashPopup-name" title={entry.name}>{entry.name}</span>
                {entry.description && (
                  <span className="jx-commandPopup-description" title={entry.description}>
                    {entry.description}
                  </span>
                )}
              </button>
            </Fragment>
          ))}
        </motion.div>
      )}
    </AnimatePresence>
  );
}

/**
 * Hook: / slash command popup visibility + keyboard nav.
 */
export function useSkillSlash() {
  const [slashVisible, setSlashVisible] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState(0);

  function handleSlashInputChange(value: string, prevValue: string) {
    const v = value.trimEnd();   // contentEditable may append \n
    const p = prevValue.trimEnd();
    if (p === '' && v === '/') {
      setSlashVisible(true);
      setSelectedIndex(0);
      return;
    }
    if (slashVisible) {
      if (v.startsWith('/') && !v.slice(1).includes(' ')) {
        setSelectedIndex(0);
      } else {
        setSlashVisible(false);
      }
    }
  }

  /** Only handles ArrowUp/Down/Escape. Enter/Tab handled by InputArea. */
  function handleSlashKeyDown(e: React.KeyboardEvent, itemCount: number): boolean {
    if (!slashVisible) return false;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setSelectedIndex((i) => Math.min(i + 1, Math.max(0, itemCount - 1)));
      return true;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      setSelectedIndex((i) => Math.max(i - 1, 0));
      return true;
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      setSlashVisible(false);
      return true;
    }
    return false;
  }

  return {
    slashVisible, setSlashVisible,
    selectedIndex, setSelectedIndex,
    handleSlashInputChange, handleSlashKeyDown,
  };
}
