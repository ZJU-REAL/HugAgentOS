export interface IndustryChainTreeNode {
  id: string;
  label: string;
  children: IndustryChainTreeNode[];
}

export interface IndustryChainMetric {
  label: string;
  value: string | number;
}

export interface IndustryChainViewModel {
  title: string;
  description: string;
  tree: IndustryChainTreeNode | null;
  nodeCount: number;
  metrics: IndustryChainMetric[];
  sections: Record<string, unknown>;
  error?: string;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function parseJson(value: unknown): unknown {
  if (typeof value !== 'string') return value;
  try {
    return JSON.parse(value) as unknown;
  } catch {
    return value;
  }
}

function unwrapOutput(output: unknown): unknown {
  let current = parseJson(output);
  for (let i = 0; i < 2; i += 1) {
    const record = asRecord(current);
    if (!record || !('result' in record)) break;
    current = parseJson(record.result);
  }
  return current;
}

function normalizeNode(value: unknown, path: string): IndustryChainTreeNode | null {
  const record = asRecord(value);
  if (!record) return null;

  const rawLabel = record['名称'] ?? record.name ?? record.label ?? record.title;
  const label = rawLabel == null ? '' : String(rawLabel).trim();
  if (!label) return null;

  const rawChildren = record['下级环节'] ?? record.children;
  const children = Array.isArray(rawChildren)
    ? rawChildren
      .map((child, index) => normalizeNode(child, `${path}.${index}`))
      .filter((child): child is IndustryChainTreeNode => child !== null)
    : [];

  return { id: path, label, children };
}

function countNodes(node: IndustryChainTreeNode | null): number {
  if (!node) return 0;
  return 1 + node.children.reduce((total, child) => total + countNodes(child), 0);
}

function toDisplayValue(value: unknown): string | number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim()) return value.trim();
  return null;
}

export function getIndustryChainNameFromInput(input: unknown): string {
  const parsed = parseJson(input);
  const record = asRecord(parsed);
  const value = record?.chain_id ?? record?.chainId ?? record?.chain ?? record?.name;
  return value == null ? '' : String(value).trim();
}

export function parseIndustryChainOutput(
  output: unknown,
  fallbackTitle = '',
): IndustryChainViewModel {
  const unwrapped = unwrapOutput(output);
  if (typeof unwrapped === 'string') {
    return {
      title: fallbackTitle || '产业链图谱',
      description: '',
      tree: null,
      nodeCount: 0,
      metrics: [],
      sections: {},
      error: unwrapped,
    };
  }

  const sections = asRecord(unwrapped) ?? {};
  const error = toDisplayValue(sections.error);
  const overview = asRecord(sections['产业链概况']) ?? {};
  const tree = normalizeNode(sections['产业链图谱'], 'root');
  const titleValue = toDisplayValue(overview['名称']);
  const descriptionValue = toDisplayValue(overview['描述']);
  const metricRecord = asRecord(overview['关键指标']) ?? {};
  const metrics = Object.entries(metricRecord)
    .map(([label, value]) => {
      const displayValue = toDisplayValue(value);
      return displayValue === null ? null : { label, value: displayValue };
    })
    .filter((metric): metric is IndustryChainMetric => metric !== null);

  return {
    title: titleValue == null ? (tree?.label || fallbackTitle || '产业链图谱') : String(titleValue),
    description: descriptionValue == null ? '' : String(descriptionValue),
    tree,
    nodeCount: countNodes(tree),
    metrics,
    sections,
    ...(error == null ? {} : { error: String(error) }),
  };
}
