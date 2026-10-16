import Ajv from 'ajv';
import schema from './application-schema.js';
import { applicationSemantics } from './semantics.js';
import type { ApplicationDeclaration, ValidationContext, ValidationIssue } from './types.js';
export type * from './types.js';
import host from './host-schema.js';
export const PROTOCOL_VERSION = host.protocolVersion;
export const UI_VERSION = host.uiVersion;
export const HOST_CAPABILITIES = host.capabilities;
const validate = new Ajv({allErrors: true, jsonPointers: true}).compile(schema);
export function validateApplication(value: unknown, context: ValidationContext = {}): ValidationIssue[] {
  if (!validate(value)) return (validate.errors || []).map(e => ({
    path: e.dataPath || '/', code: 'structure', message: 'Invalid field structure',
  }));
  const app = value as ApplicationDeclaration;
  return applicationSemantics(app, {...context, capabilities: context.capabilities ?? HOST_CAPABILITIES});
}

export {validateContributions} from './contributions.js';
export type {ValidatedContributions} from './contributions.js';
