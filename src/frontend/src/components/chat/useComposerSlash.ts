import { useState } from 'react';

/**
 * Hook: / slash command popup visibility + keyboard nav.
 */
export function useSkillSlash() {
  const [slashVisible, setSlashVisible] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState(0);

  function handleSlashInputChange(value: string, prevValue: string) {
    const v = value.trimEnd();   // contentEditable may append \n
    const p = prevValue.trimEnd();
    if (p === '' && /^\/[^\s]*$/.test(v)) {
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
