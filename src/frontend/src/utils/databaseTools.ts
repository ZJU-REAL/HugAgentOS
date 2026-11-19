import { t } from '../i18n';
import { useCatalogStore } from '../stores/catalogStore';
import type { MCPItem } from '../types/capabilities';

/**
 * Database tools: each installed data source connector exposes tools named
 * `<prefix>__<suffix>`. A tool is a database tool when a catalog connector owned
 * by the data-source feature lists it; for chat history whose connector is gone,
 * the suffixes only the database MCP uses still identify it.
 */
export interface DatabaseToolLabels {
  /** Step label in the tool row. */
  step: string;
  running: string;
  done: string;
}

const QUERY: DatabaseToolLabels = { step: t('数据库查询'), running: t('正在查询数据库'), done: t('查询了数据库') };
const LIST: DatabaseToolLabels = { step: t('查看库表'), running: t('正在查看库表'), done: t('查看了库表') };
const DESCRIBE: DatabaseToolLabels = { step: t('查看表结构'), running: t('正在查看表结构'), done: t('查看了表结构') };

/** The one per-suffix table: every database MCP tool, and how a call to it reads. */
const LABELS: Record<string, DatabaseToolLabels> = {
  get_business_context: { step: t('读取数据字典'), running: t('正在读取数据字典'), done: t('读取了数据字典') },
  list_schemas: LIST,
  list_tables: LIST,
  list_indices: LIST,
  describe_table: DESCRIBE,
  get_mappings: DESCRIBE,
  sample_rows: { step: t('查看样例数据'), running: t('正在查看样例数据'), done: t('查看了样例数据') },
  explain_query: { step: t('查看执行计划'), running: t('正在查看执行计划'), done: t('查看了执行计划') },
  execute_write_sql: { step: t('写入数据库'), running: t('正在写入数据库'), done: t('写入了数据库') },
  execute_sql: QUERY,
  esql: QUERY,
  search: QUERY,
  get_document: QUERY,
};

/** Suffixes other MCP servers use too: trusted only when a database connector owns the tool. */
const SHARED_SUFFIXES = new Set(['search', 'get_document']);
const DATASOURCES = 'datasources';

let ownedFor: MCPItem[] | undefined;
let ownedNames = new Set<string>();

/** Tool names of the data-source connectors in the current catalog (memoized per catalog). */
function databaseConnectorTools(): Set<string> {
  const mcp = (useCatalogStore.getState().catalog?.mcp || []) as MCPItem[];
  if (mcp !== ownedFor) {
    ownedFor = mcp;
    ownedNames = new Set(mcp.filter((item) => item.managed_by === DATASOURCES).flatMap((item) => item.tools || []));
  }
  return ownedNames;
}

/** The suffix of a database MCP tool (`list_tables`, `execute_sql`…), or null for any other tool. */
export function databaseToolSuffix(name: string): string | null {
  const i = name.lastIndexOf('__');
  if (i <= 0) return null;
  const suffix = name.slice(i + 2);
  if (!(suffix in LABELS)) return null;
  if (databaseConnectorTools().has(name)) return suffix;
  return SHARED_SUFFIXES.has(suffix) ? null : suffix;
}

export function databaseToolLabels(suffix: string): DatabaseToolLabels {
  return LABELS[suffix] || QUERY;
}
