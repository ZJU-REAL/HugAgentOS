import { useEffect } from 'react';

interface Props {
  chatId: string;
  refreshKey?: number;
  onLevelChange?: (...args: any[]) => void;
}

export function ChatShareBanner({ chatId, refreshKey, onLevelChange }: Props) {
  useEffect(() => {
    onLevelChange?.(null, null);
  }, [chatId, onLevelChange, refreshKey]);
  return null;
}
