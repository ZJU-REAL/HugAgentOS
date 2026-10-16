import Ajv from 'ajv';
import moduleSchema from './module-schema.js';
import applicationSchema from './application-schema.js';
import {validateApplication, HOST_CAPABILITIES} from './index.js';
import type {ApplicationDeclaration, ValidationContext, ValidationIssue} from './types.js';
import type {ModuleContribution} from './legacy.js';
const validateModule = new Ajv({allErrors: true, jsonPointers: true}).compile(moduleSchema);
export interface ValidatedContributions {
  applications: ApplicationDeclaration[];
  modules: ModuleContribution[];
  errors: ValidationIssue[];
}
export function validateContributions(value: unknown, context: ValidationContext = {}): ValidatedContributions {
  const result: ValidatedContributions = {applications: [], modules: [], errors: []};
  const error = (path: string, code: string, message: string) => result.errors.push({path, code, message});
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    error('/', 'structure', 'Expected contributions object'); return result;
  }
  const input = value as Record<string, unknown>;
  for (const kind of ['modules', 'applications'] as const) {
    if (input[kind] !== undefined && (!Array.isArray(input[kind]) || input[kind].length > 200)) error('/' + kind, 'structure', 'Expected at most 200 entries');
  }
  const entries = (kind: string): unknown[] => Array.isArray(input[kind]) && input[kind].length <= 200 ? input[kind] : [];
  const duplicate = (list: unknown[], item: unknown) => {
    const id = item && typeof item === 'object' ? (item as {id?: unknown}).id : undefined;
    return id !== undefined && list.filter(e => e && typeof e === 'object' && (e as {id?: unknown}).id === id).length > 1;
  };
  for (const [index, module] of entries('modules').entries()) {
    const prefix = '/modules/' + index;
    if (!validateModule(module)) {
      for (const issue of validateModule.errors || []) error(prefix + (issue.dataPath || ''), 'structure', 'Invalid field structure');
      continue;
    }
    const item = module as ModuleContribution;
    if (duplicate(entries('modules'), item)) { error(prefix + '/id', 'duplicate_id', 'Duplicate module ID'); continue; }
    let valid = true;
    for (const [i, grant] of (item.grants || []).entries()) {
      if (!(context.capabilities ?? HOST_CAPABILITIES).includes(grant) && !grant.startsWith('data_source:')) {
        error(prefix + '/grants/' + i, 'unsupported_capability', 'Required capability is unavailable'); valid = false;
      }
      if (grant.startsWith('data_source:') && entries('data_sources').filter(source => source && typeof source === 'object' && (source as {id?: unknown}).id === grant.slice(12)).length !== 1) {
        error(prefix + '/grants/' + i, 'invalid_reference', 'Unknown data source'); valid = false;
      }
    }
    if (valid) result.modules.push({...item, title: item.title ?? item.id, grants: item.grants ?? []});
  }
  for (const [index, app] of entries('applications').entries()) {
    const prefix = '/applications/' + index;
    if (duplicate(entries('applications'), app)) { error(prefix + '/id', 'duplicate_id', 'Duplicate application ID'); continue; }
    const issues = validateApplication(app, {...context, modules: result.modules});
    result.errors.push(...issues.map(issue => ({...issue, path: prefix + issue.path})));
    if (!issues.length) {
      const input = app as ApplicationDeclaration;
      const projected = Object.fromEntries(Object.entries(input).filter(([key]) => key in applicationSchema.properties));
      if (input.compatibility) projected.compatibility = Object.fromEntries(
        Object.entries(input.compatibility).filter(([key]) => key in applicationSchema.properties.compatibility.properties));
      result.applications.push(projected as unknown as ApplicationDeclaration);
    }
  }
  return result;
}
