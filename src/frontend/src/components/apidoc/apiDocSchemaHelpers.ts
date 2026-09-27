import type { ApiComponents, JsonValue, SchemaDefinition } from './apiDocModel';

export function resolveRef(ref: string, components: ApiComponents): SchemaDefinition | undefined {
  const parts = ref.replace(/^#\//, '').split('/');
  let cur: unknown = { components };
  for (const p of parts) {
    if (cur && typeof cur === 'object') cur = (cur as Record<string, unknown>)[p];
    else return undefined;
  }
  return cur && typeof cur === 'object' ? cur as SchemaDefinition : undefined;
}

export function typeLabel(def?: SchemaDefinition): string {
  if (!def) return '?';
  if (def.$ref) return def.$ref.split('/').pop() || 'ref';
  if (def.type === 'array') {
    const inner = def.items?.type || (def.items?.$ref ? def.items.$ref.split('/').pop() : '?');
    return `array<${inner}>`;
  }
  if (Array.isArray(def.anyOf)) return 'anyOf';
  if (Array.isArray(def.oneOf)) return 'oneOf';
  if (Array.isArray(def.allOf)) return 'allOf';
  return def.type || (def.format ? def.format : '?');
}

export function generateSample(schema: SchemaDefinition | undefined, components: ApiComponents, visited?: Set<string>): JsonValue {
  const v = visited ?? new Set<string>();
  if (!schema) return null;

  if (schema.$ref) {
    if (v.has(schema.$ref)) return null;
    const next = new Set(v);
    next.add(schema.$ref);
    return generateSample(resolveRef(schema.$ref, components), components, next);
  }
  if (schema.example !== undefined) return schema.example;
  if (schema.default !== undefined) return schema.default;
  if (Array.isArray(schema.enum) && schema.enum.length > 0) return schema.enum[0];
  if (Array.isArray(schema.allOf)) {
    const merged: Record<string, JsonValue> = {};
    for (const s of schema.allOf) Object.assign(merged, generateSample(s, components, v) || {});
    return merged;
  }
  if (Array.isArray(schema.anyOf) && schema.anyOf.length > 0) return generateSample(schema.anyOf[0], components, v);
  if (Array.isArray(schema.oneOf) && schema.oneOf.length > 0) return generateSample(schema.oneOf[0], components, v);
  if (schema.type === 'array') return [generateSample(schema.items, components, v)];
  if (schema.type === 'object' || schema.properties) {
    const out: Record<string, JsonValue> = {};
    for (const [k, def] of Object.entries(schema.properties || {})) {
      out[k] = generateSample(def, components, v);
    }
    return out;
  }
  switch (schema.type) {
    case 'string':
      if (schema.format === 'date-time') return '2025-01-01T00:00:00Z';
      if (schema.format === 'date') return '2025-01-01';
      if (schema.format === 'uuid') return '00000000-0000-0000-0000-000000000000';
      return 'string';
    case 'integer': return 0;
    case 'number': return 0;
    case 'boolean': return false;
    default: return null;
  }
}
