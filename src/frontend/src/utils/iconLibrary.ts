// Default icon library served by nginx from src/frontend/public/home/.
// Update the arrays below if new icons are added to the public folders.

export const APP_ICON_LIBRARY: string[] = Array.from(
  { length: 30 },
  (_, i) => `/home/random-icons/Frame ${442 + i}.svg`,
);

export const MCP_ICON_LIBRARY: string[] = [
  '/home/mcp/internet.svg',
  '/home/mcp/industry-chain.svg',
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
