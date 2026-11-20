import { useCallback, useEffect, useRef, useState } from 'react';
import { message } from 'antd';

import { usePolling } from '../../hooks/usePolling';

/** Every device-flow account status carries at least these states. */
interface DeviceFlowStatusBase {
  status: string;
}

export interface DeviceFlowApi<S extends DeviceFlowStatusBase, P = void> {
  /** `probe=true` asks the backend for a real API liveness check instead of the cached DB state. */
  getStatus: (probe: boolean) => Promise<S>;
  start: () => Promise<S>;
  poll: (arg: P | undefined) => Promise<S>;
  disconnect: () => Promise<S>;
}

interface DeviceFlowMessages {
  loadFailed: string;
  startFailed: string;
  connected: string;
  disconnected: string;
  disconnectFailed: string;
}

interface UseDeviceFlowConnectionOptions<S extends DeviceFlowStatusBase, P = void> {
  api: DeviceFlowApi<S, P>;
  messages: DeviceFlowMessages;
  /** Delay between two pending polls. A long-poll backend blocks inside the call; ticks never overlap. */
  pollInterval?: number;
  /** Merge a poll response into the shown status (e.g. keep a QR code the response left out). */
  mergePolled?: (prev: S | null, next: S) => S;
  /** Called with the status returned by `start`, to warn about a missing QR code and similar. */
  onStarted?: (status: S) => void;
}

const LIVENESS_INTERVAL = 60_000;

/**
 * State machine shared by the Feishu / DingTalk / Yida account panels:
 * one real liveness probe on open, a silent probe every minute while connected,
 * and polling while the device-flow login is pending. All timers pause while
 * the page is hidden and stop on unmount.
 */
export function useDeviceFlowConnection<S extends DeviceFlowStatusBase, P = void>({
  api,
  messages,
  pollInterval = 3000,
  mergePolled,
  onStarted,
}: UseDeviceFlowConnectionOptions<S, P>) {
  const [status, setStatus] = useState<S | null>(null);
  const [loading, setLoading] = useState(false);
  const [connecting, setConnecting] = useState(false);
  // Bumped by disconnect / a new login so a poll that is still in flight cannot
  // overwrite the newer state when it finally returns.
  const generation = useRef(0);
  const pollArg = useRef<P | undefined>(undefined);
  const apiRef = useRef(api);
  const optionsRef = useRef({ messages, mergePolled, onStarted });
  useEffect(() => {
    apiRef.current = api;
    optionsRef.current = { messages, mergePolled, onStarted };
  });

  const refresh = useCallback(async (probe = false, silent = false) => {
    if (!silent) setLoading(true);
    try {
      setStatus(await apiRef.current.getStatus(probe));
    } catch {
      if (!silent) message.error(optionsRef.current.messages.loadFailed);
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  // The local DB may still say "connected" while the credentials are already invalid: probe once on open.
  useEffect(() => { void refresh(true); }, [refresh]);

  const current = status?.status;
  usePolling(() => refresh(true, true), {
    interval: LIVENESS_INTERVAL,
    enabled: current === 'connected',
  });

  const pollOnce = useCallback(async () => {
    const gen = generation.current;
    let next: S;
    try {
      next = await apiRef.current.poll(pollArg.current);
    } catch {
      return; // A single failed poll is retried on the next tick.
    }
    if (gen !== generation.current) return;
    const { mergePolled: merge, messages: texts } = optionsRef.current;
    setStatus((prev) => (merge ? merge(prev, next) : next));
    if (next.status === 'connected') message.success(texts.connected);
  }, []);
  usePolling(pollOnce, { interval: pollInterval, enabled: current === 'pending' });

  const connect = useCallback(async () => {
    generation.current += 1;
    pollArg.current = undefined;
    setConnecting(true);
    try {
      const next = await apiRef.current.start();
      setStatus(next);
      optionsRef.current.onStarted?.(next);
    } catch {
      message.error(optionsRef.current.messages.startFailed);
    } finally {
      setConnecting(false);
    }
  }, []);

  /** Resume pending polling with an argument, e.g. the organization the user picked. */
  const continueWith = useCallback((arg: P) => {
    pollArg.current = arg;
    setStatus((prev) => (prev ? { ...prev, status: 'pending' } : prev));
  }, []);

  const disconnect = useCallback(async () => {
    generation.current += 1;
    try {
      setStatus(await apiRef.current.disconnect());
      message.success(optionsRef.current.messages.disconnected);
    } catch {
      message.error(optionsRef.current.messages.disconnectFailed);
    }
  }, []);

  return { status, loading, connecting, refresh, connect, continueWith, disconnect };
}
