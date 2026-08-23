/**
 * Field-mapping pointer evaluator.
 *
 * This is a **whitelist parser, not an expression engine** — no `eval`, no
 * function bodies, no property access outside the literal grammar below. That
 * limitation is the reason a plugin's declaration can be treated as data rather
 * than as code, which is what makes the whole L0 layer safe to accept from a
 * manifest.
 *
 *   $                    the current value
 *   $.field              a property
 *   $.a.b                nested properties
 *   $.items[]            an array
 *   $.items[].name       a property of each array element (column mapping)
 *   $root. / $node. / $item.   action scopes (see resolveParams)
 */

type Json = unknown;

const SCOPE_PREFIXES = ['$root', '$node', '$item'] as const;
export type PointerScope = typeof SCOPE_PREFIXES[number];

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/**
 * Read one **own** property.
 *
 * Going through the prototype chain would let a declaration name `constructor`
 * (or anything else inherited) and pull a built-in out of the host. Data read
 * from a manifest must only ever see data the payload actually carries, so
 * inherited keys resolve to undefined like any other miss.
 */
function ownProperty(source: Record<string, unknown>, key: string): unknown {
  return Object.prototype.hasOwnProperty.call(source, key) ? source[key] : undefined;
}

/** Parse a JSON string once; non-strings pass through untouched. */
export function parseMaybeJson(value: unknown): unknown {
  if (typeof value !== 'string') return value;
  const trimmed = value.trim();
  if (!trimmed || (trimmed[0] !== '{' && trimmed[0] !== '[')) return value;
  try {
    return JSON.parse(trimmed) as unknown;
  } catch {
    return value;
  }
}

/**
 * Peel wrapper keys off a tool result.
 *
 * MCP results routinely arrive wrapped (`{result: "...json..."}`,
 * `{结果: {...}}`). A plugin lists the wrappers it expects and the host unwraps
 * them, so the manifest's pointers describe the business payload rather than
 * the transport envelope.
 */
export function unwrap(value: unknown, keys: string[] | undefined): unknown {
  let current = parseMaybeJson(value);
  if (!keys || keys.length === 0) return current;
  for (let depth = 0; depth < 6; depth += 1) {
    const record = isRecord(current) ? current : null;
    if (!record) break;
    const hit = keys.find((key) => Object.prototype.hasOwnProperty.call(record, key));
    if (!hit) break;
    current = parseMaybeJson(record[hit]);
  }
  return current;
}

interface Step {
  key: string;
  spread: boolean;
}

function parseSteps(pointer: string): { scope: PointerScope | null; steps: Step[] } | null {
  const text = (pointer || '').trim();
  if (!text.startsWith('$')) return null;

  let scope: PointerScope | null = null;
  let rest = text.slice(1);
  for (const prefix of SCOPE_PREFIXES) {
    const name = prefix.slice(1);
    if (rest === name || rest.startsWith(`${name}.`)) {
      scope = prefix;
      rest = rest.slice(name.length);
      break;
    }
  }
  if (!rest) return { scope, steps: [] };
  if (!rest.startsWith('.')) return null;

  const steps: Step[] = [];
  for (const raw of rest.slice(1).split('.')) {
    if (!raw) return null;
    const spread = raw.endsWith('[]');
    const key = spread ? raw.slice(0, -2) : raw;
    if (!key || key.includes('[') || key.includes(']')) return null;
    steps.push({ key, spread });
  }
  return { scope, steps };
}

/** Resolve one pointer against a value. Returns undefined when it does not match. */
export function readPointer(value: unknown, pointer: string): unknown {
  const parsed = parseSteps(pointer);
  if (!parsed) return undefined;

  let current: Json = value;
  for (const step of parsed.steps) {
    if (Array.isArray(current)) {
      // `$.items[].name` — map the remaining key across the array.
      current = current
        .map((item) => (isRecord(item) ? ownProperty(item, step.key) : undefined))
        .filter((item) => item !== undefined);
      continue;
    }
    if (!isRecord(current)) return undefined;
    current = ownProperty(current, step.key);
    if (step.spread) {
      current = Array.isArray(current) ? current : parseMaybeJson(current);
      if (!Array.isArray(current)) return undefined;
    }
  }
  return current;
}

/** Read a pointer, or return the literal when the string is not a pointer. */
export function readPointerOrLiteral(value: unknown, spec: unknown): unknown {
  if (typeof spec !== 'string') return spec;
  if (!spec.startsWith('$')) return spec;
  return readPointer(value, spec);
}

/** Read the first pointer in `specs` that resolves to something. */
export function readFirst(value: unknown, specs: unknown): unknown {
  const list = Array.isArray(specs) ? specs : [specs];
  for (const spec of list) {
    const result = readPointerOrLiteral(value, spec);
    if (result !== undefined && result !== null && result !== '') return result;
  }
  return undefined;
}

/** A pointer's value coerced to display text ('' when absent). */
export function readText(value: unknown, spec: unknown): string {
  const result = readFirst(value, spec);
  if (result === undefined || result === null) return '';
  if (typeof result === 'string') return result;
  if (typeof result === 'number' || typeof result === 'boolean') return String(result);
  if (Array.isArray(result)) return result.filter((x) => x != null).map(String).join('、');
  return '';
}

/** A pointer's value coerced to a finite number (undefined when not numeric). */
export function readNumber(value: unknown, spec: unknown): number | undefined {
  const result = readFirst(value, spec);
  if (typeof result === 'number' && Number.isFinite(result)) return result;
  if (typeof result === 'string') {
    // Upstream figures often arrive decorated ("1,234", "23.5%", "12亿元").
    const cleaned = result.replace(/[,\s]/g, '').match(/-?\d+(\.\d+)?/);
    if (cleaned) {
      const parsed = Number(cleaned[0]);
      if (Number.isFinite(parsed)) return parsed;
    }
  }
  return undefined;
}

/** A pointer's value as an array of records (empty when it is not a list). */
export function readRecords(value: unknown, spec: unknown): Array<Record<string, unknown>> {
  const result = readFirst(value, spec);
  if (!Array.isArray(result)) return [];
  return result.filter(isRecord);
}

/** A pointer's value as a plain array (of anything). */
export function readArray(value: unknown, spec: unknown): unknown[] {
  const result = readFirst(value, spec);
  return Array.isArray(result) ? result : [];
}

/** A pointer's value as a record (empty when it is not an object). */
export function readRecord(value: unknown, spec: unknown): Record<string, unknown> {
  const result = spec === undefined ? value : readFirst(value, spec);
  return isRecord(result) ? result : {};
}

/**
 * Resolve an action's `params` against the scopes available at click time.
 *
 * `$root.x` reads the whole tool output, `$node.x` the clicked graph node and
 * `$item.x` the clicked list row; a bare `$.x` also reads the root, which is
 * the common case for canvas-level actions.
 */
export function resolveParams(
  params: Record<string, string> | undefined,
  scopes: { root?: unknown; node?: unknown; item?: unknown },
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, spec] of Object.entries(params || {})) {
    if (typeof spec !== 'string') continue;
    if (!spec.startsWith('$')) {
      out[key] = spec;
      continue;
    }
    const parsed = parseSteps(spec);
    if (!parsed) continue;
    const target =
      parsed.scope === '$node' ? scopes.node
      : parsed.scope === '$item' ? scopes.item
      : scopes.root;
    const stripped = parsed.scope ? `$${spec.slice(parsed.scope.length)}` : spec;
    const value = readPointer(target, stripped === '$' ? '$' : stripped);
    if (value !== undefined) out[key] = value;
  }
  return out;
}
