/** Shared command recognition for the composer and wire-message decoration. */
export function isProjectInitCommand(text: string): boolean {
  return ['/init', '/初始化指令'].includes(text.trim());
}

export function canInitializeProject(options: {
  projectId?: string | null;
  permission?: string;
  busy: boolean;
  hasCapability: boolean;
  specialMode: boolean;
}): boolean {
  return !!options.projectId
    && (options.permission === 'admin' || options.permission === 'edit')
    && !options.busy && !options.hasCapability && !options.specialMode;
}
