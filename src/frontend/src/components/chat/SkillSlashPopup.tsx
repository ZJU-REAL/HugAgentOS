import { useEffect, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { AppstoreOutlined, BulbOutlined } from '@ant-design/icons';
import { usePopupFlip } from '../../hooks/usePopupFlip';
import { t } from '../../i18n';
import type { InstalledPluginItem } from '../../types';

export type SlashEntry = {
  kind: 'skill' | 'plugin';
  id: string;
  name: string;
  plugin?: InstalledPluginItem;
};

interface SkillSlashPopupProps {
  entries: SlashEntry[];
  visible: boolean;
  selectedIndex: number;
  onSelect: (entry: SlashEntry) => void;
  onHover: (index: number) => void;
}

export function SkillSlashPopup({ entries, visible, selectedIndex, onSelect, onHover }: SkillSlashPopupProps) {
  const itemRefs = useRef<(HTMLDivElement | null)[]>([]);
  const popupRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (visible && itemRefs.current[selectedIndex]) {
      itemRefs.current[selectedIndex]!.scrollIntoView({ block: 'nearest' });
    }
  }, [selectedIndex, visible]);

  const showPopup = visible && entries.length > 0;
  // Not enough space above (e.g. the project detail page input box is near the top of the page) -> flip to below the cursor's line
  const { below: flipBelow, belowTop } = usePopupFlip(popupRef, showPopup);

  return (
    <AnimatePresence>
      {showPopup && (
        <motion.div
          ref={popupRef}
          className={`jx-slashPopup${flipBelow ? ' jx-slashPopup--below' : ''}`}
          style={flipBelow && belowTop != null ? { top: belowTop } : undefined}
          onMouseDown={(e) => e.preventDefault()}
          initial={{ opacity: 0, y: flipBelow ? -6 : 6, scale: 0.97 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: flipBelow ? -4 : 4, scale: 0.97 }}
          transition={{ duration: 0.16, ease: 'easeOut' }}
        >
          {entries.map((entry, idx) => (
            <div
              key={`${entry.kind}-${entry.id}`}
              ref={(el) => { itemRefs.current[idx] = el; }}
              className={`jx-slashPopup-item${idx === selectedIndex ? ' active' : ''}`}
              onMouseEnter={() => onHover(idx)}
              onClick={() => onSelect(entry)}
            >
              {entry.kind === 'plugin'
                ? <AppstoreOutlined className="jx-slashPopup-icon jx-slashPopup-icon--plugin" />
                : <BulbOutlined className="jx-slashPopup-icon jx-slashPopup-icon--skill" />}
              <span className="jx-slashPopup-name">{entry.name}</span>
              {entry.kind === 'plugin' && <span className="jx-slashPopup-badge">{t('插件')}</span>}
            </div>
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
  function handleSlashKeyDown(e: React.KeyboardEvent): boolean {
    if (!slashVisible) return false;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setSelectedIndex((i) => i + 1); // clamped by popup render
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
