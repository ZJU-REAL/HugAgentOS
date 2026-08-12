// Default icon library served by nginx from src/frontend/public/home/.
// Update the arrays below if new icons are added to the public folders.

export const APP_ICON_LIBRARY: string[] = Array.from(
  { length: 30 },
  (_, i) => `/home/random-icons/Frame ${442 + i}.svg`,
);

export const MCP_ICON_LIBRARY: string[] = [
  '/home/mcp/internet.svg',
  '/home/mcp/data.svg',
  '/home/mcp/database.svg',
  '/home/mcp/knowledge.svg',
  '/home/mcp/learning.svg',
  '/home/mcp/report.svg',
  '/home/mcp/source.svg',
  '/home/mcp/format-painter.svg',
  '/home/mcp/list.svg',
  '/home/mcp/on.svg',
  '/home/mcp/off.svg',
];

const LEGACY_MCP_ICON_PATHS: Record<string, string> = {
  '/home/mcp/互联网.svg': '/home/mcp/internet.svg',
  '/home/mcp/数据.svg': '/home/mcp/data.svg',
  '/home/mcp/数据库.svg': '/home/mcp/database.svg',
  '/home/mcp/知识.svg': '/home/mcp/knowledge.svg',
  '/home/mcp/报告.svg': '/home/mcp/report.svg',
  '/home/mcp/来源.svg': '/home/mcp/source.svg',
};

/** Keep old DB rows renderable while backend startup repairs their icon paths. */
export function normalizeMcpIconUrl(icon?: string): string {
  const value = String(icon || '').trim();
  return LEGACY_MCP_ICON_PATHS[value] || value;
}
