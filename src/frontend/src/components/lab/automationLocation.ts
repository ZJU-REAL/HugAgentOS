export type ExecutionLocation = 'local' | 'cloud';

export function defaultExecutionLocation(mode: string, projectKind?: string): ExecutionLocation {
  return mode === 'local_only' || (mode === 'dual' && projectKind === 'local') ? 'local' : 'cloud';
}
export function executionLocationError(location: ExecutionLocation, projectKind?: string, prompt = ''): string | null {
  if (projectKind && (projectKind === 'local') !== (location === 'local')) {
    return '项目与执行位置不匹配，本机目录必须在本机执行';
  }
  const hostPath = /(?:(?<![A-Za-z0-9])[A-Za-z]:[\\/]|\/Users\/|\/home\/|\/mnt\/)/.test(prompt);
  if (hostPath && location === 'cloud') return '云端任务不能直接访问本机目录，请选择本机执行并绑定项目';
  if (hostPath && !projectKind) return '读取本机目录的定时任务必须绑定本机项目';
  return null;
}

/** Use the runtime's IANA data rather than a list of developer locations. */
export function currentTimezone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
}

export function availableTimezones(): string[] {
  const runtime = Intl as typeof Intl & { supportedValuesOf?: (key: 'timeZone') => string[] };
  return [...new Set([currentTimezone(), 'UTC', ...(runtime.supportedValuesOf?.('timeZone') || [])])];
}
