import type { ApplicationDeclaration, ValidationContext, ValidationIssue } from './types.js';
export function applicationSemantics(app: ApplicationDeclaration, context: ValidationContext): ValidationIssue[] {
  const errors: ValidationIssue[] = [];
  const reject = (path: string, code: string, message: string) => errors.push({path, code, message});
  const target = app.target;
  if (context.source === 'plugin' && target.kind !== 'plugin') reject('/target/kind', 'untrusted_target', 'Plugin applications must reference their own modules');
  if (target.kind === 'builtin' && !(context.componentIds || []).includes(target.componentId)) reject('/target/componentId', 'unknown_component', 'Unknown component');
  if (target.kind === 'command' && !(context.commandIds || []).includes(target.commandId)) reject('/target/commandId', 'unknown_command', 'Unknown command');
  const moduleExists = (id: string) => {
    const matches = (context.modules || []).filter(m => m.id === id);
    return matches.length === 1 && matches[0].surface === 'workspace';
  };
  if (target.kind === 'plugin' && !moduleExists(target.moduleId)) reject('/target/moduleId', 'invalid_module', 'Expected one workspace module');
  const ids = new Set<string>();
  const paths = new Set<string>();
  for (const [index, view] of (app.views || []).entries()) {
    if (ids.has(view.id)) reject('/views/' + index + '/id', 'duplicate_id', 'Duplicate view ID');
    if (paths.has(view.path)) reject('/views/' + index + '/path', 'duplicate_path', 'Duplicate view path');
    ids.add(view.id); paths.add(view.path);
    if (view.moduleId && (target.kind !== 'plugin' || !moduleExists(view.moduleId))) reject('/views/' + index + '/moduleId', 'invalid_module', 'Expected one workspace module');
  }
  if (target.kind === 'external') {
    try {
      const authority = target.url.split('://')[1].split(/[/?#]/)[0];
      const numericHost = authority.split(':')[0];
      if (/^[0-9.]+$/.test(numericHost) && numericHost.split('.').some(part =>
        !/^(0|[1-9][0-9]{0,2})$/.test(part) || Number(part) > 255)) throw new Error();
      const url = new URL(target.url);
      if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password || target.url.includes('\\')) throw new Error();
    } catch { reject('/target/url', 'unsafe_url', 'Expected an HTTP URL without credentials'); }
  }
  const version = app.compatibility?.protocolVersion;
  if (version && (version.split('.')[0] !== '1' || Number(version.split('.')[1]) > 0)) reject('/compatibility/protocolVersion', 'incompatible_version', 'Unsupported protocol version');
  for (const [index, capability] of (app.compatibility?.requiredCapabilities || []).entries()) {
    if (!(context.capabilities || []).includes(capability)) reject('/compatibility/requiredCapabilities/' + index, 'unsupported_capability', 'Required capability is unavailable');
  }
  return errors;
}
