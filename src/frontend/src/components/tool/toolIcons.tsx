import type { ReactNode } from 'react';
import type { ToolIconKind } from '../../utils/toolIconKind';

/**
 * Line icons for host-owned tools.
 *
 * One icon per kind of action, not per tool function: the three knowledge-base
 * retrievers share the book, every shell entry point shares the terminal.
 * Drawn on a 16px canvas with a 2px safe area, 1.5 stroke, round caps and
 * joins, no fill, `currentColor` — so a row tints the icon by colour alone
 * (grey when done, brand blue while running).
 *
 * Tools contributed by skills, plugins and MCP connectors keep their own logo
 * (`ToolCapabilityIcon`); these icons are only for what the host itself ships.
 */
const DOC = 'M9.5 2H5A1.5 1.5 0 0 0 3.5 3.5v9A1.5 1.5 0 0 0 5 14h6a1.5 1.5 0 0 0 1.5-1.5V5z';
const FOLDER = 'M2 12V4.5A1.5 1.5 0 0 1 3.5 3h2.4l1.5 1.5h5.1A1.5 1.5 0 0 1 14 6v6a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 12z';
const BUBBLE = 'M3.5 2.75h9A1.5 1.5 0 0 1 14 4.25v5.5a1.5 1.5 0 0 1-1.5 1.5H8.5L5.5 13.5v-2.25h-2A1.5 1.5 0 0 1 2 9.75v-5.5a1.5 1.5 0 0 1 1.5-1.5z';

const PATHS: Record<ToolIconKind, ReactNode> = {
  terminal: <><rect x="2" y="3" width="12" height="10" rx="2" /><path d="M5 6.5 7 8.25 5 10" /><path d="M8.5 10H11" /></>,
  fileRead: <><path d={DOC} /><path d="M9.5 2v3h3" /><path d="M6 8.5h4M6 11h2.5" /></>,
  fileWrite: <><path d={DOC} /><path d="M9.5 2v3h3" /><path d="M8 7.75v3.5M6.25 9.5h3.5" /></>,
  edit: <><path d="M10.2 3.3a1.41 1.41 0 0 1 2 2L6.5 11l-2.5.75.75-2.5z" /><path d="m9.2 4.3 2 2" /><path d="M9 13.5h4" /></>,
  findFiles: <><path d="M2 11.5v-7A1.5 1.5 0 0 1 3.5 3h2.4l1.5 1.5h5.1A1.5 1.5 0 0 1 14 6v1.25" /><path d="M2 11.5A1.5 1.5 0 0 0 3.5 13H7" /><circle cx="11" cy="10.5" r="2.25" /><path d="m12.6 12.1 1.4 1.4" /></>,
  grep: <><path d="M2.5 4h9M2.5 7h5M2.5 10h3" /><circle cx="10.5" cy="10" r="2.4" /><path d="m12.2 11.7 1.8 1.8" /></>,
  image: <><rect x="2" y="3" width="12" height="10" rx="2" /><circle cx="10.25" cy="6.25" r="1.1" /><path d="m2.5 11.5 3.25-3.25 2.75 2.75 1.75-1.75 3.25 3" /></>,
  webSearch: <><circle cx="7" cy="7" r="4.5" /><path d="m10.3 10.3 3.2 3.2" /></>,
  globe: <><circle cx="8" cy="8" r="5.75" /><path d="M2.25 8h11.5" /><path d="M8 2.25C6.4 3.85 5.6 5.75 5.6 8s.8 4.15 2.4 5.75c1.6-1.6 2.4-3.5 2.4-5.75S9.6 3.85 8 2.25z" /></>,
  knowledge: <><path d="M3 12.5v-9A1.5 1.5 0 0 1 4.5 2H13v9H4.5a1.5 1.5 0 0 0 0 3H13" /><path d="M6.5 5.5H10" /></>,
  graph: <><circle cx="4.5" cy="4.5" r="1.75" /><circle cx="11.75" cy="5.5" r="1.75" /><circle cx="7.5" cy="12" r="1.75" /><path d="m6.2 4.75 3.8.5M5.1 6.2l1.6 4.1M10.8 7l-2.3 3.4" /></>,
  database: <><ellipse cx="8" cy="4" rx="5" ry="1.75" /><path d="M3 4v8c0 .97 2.24 1.75 5 1.75s5-.78 5-1.75V4" /><path d="M3 8c0 .97 2.24 1.75 5 1.75s5-.78 5-1.75" /></>,
  skill: <path d="M9 1.75 3.75 9H8l-1 5.25L12.25 7H8z" />,
  plugin: <><path d="M6 2v3M10 2v3" /><path d="M4.5 5h7v2.25a3.5 3.5 0 0 1-7 0z" /><path d="M8 10.75V14" /></>,
  catalog: <><rect x="2.5" y="2.5" width="4.5" height="4.5" rx="1.2" /><rect x="9" y="2.5" width="4.5" height="4.5" rx="1.2" /><rect x="2.5" y="9" width="4.5" height="4.5" rx="1.2" /><rect x="9" y="9" width="4.5" height="4.5" rx="1.2" /></>,
  agent: <><rect x="2.75" y="5" width="10.5" height="8.25" rx="2.5" /><path d="M8 5V2.5" /><path d="M6 8.5v1.25M10 8.5v1.25" /></>,
  ask: <><path d={BUBBLE} /><path d="M6.6 5.6a1.45 1.45 0 1 1 2 1.35c-.38.16-.6.48-.6.9" /><path d="M8 9.5v.01" /></>,
  job: <><path d="M8 2.25 13.75 5.25 8 8.25 2.25 5.25z" /><path d="m2.25 8.25 5.75 3 5.75-3" /><path d="m2.25 11.25 5.75 3 5.75-3" /></>,
  folderUp: <><path d={FOLDER} /><path d="M8 11.25v-3.5M6.4 9.35 8 7.75l1.6 1.6" /></>,
  folderDown: <><path d={FOLDER} /><path d="M8 7.5V11M6.4 9.4 8 11l1.6-1.6" /></>,
  pin: <><path d="M6 2.25h4" /><path d="M6.75 2.25v3.5L4.75 8.5h6.5l-2-2.75v-3.5" /><path d="M8 8.5v5.25" /></>,
  word: <><path d={DOC} /><path d="M9.5 2v3h3" /><path d="m5.6 8 1 3.5L8 9.1l1.4 2.4 1-3.5" /></>,
  table: <><rect x="2.25" y="3" width="11.5" height="10" rx="1.5" /><path d="M2.25 6.5h11.5M2.25 9.75h11.5M6.25 6.5V13" /></>,
  publish: <><rect x="2" y="2.5" width="12" height="11" rx="1.75" /><path d="M2 5.5h12" /><path d="M8 11.5V8M6.4 9.6 8 8l1.6 1.6" /></>,
  chats: <><path d={BUBBLE} /><path d="M8 4.75V7l1.5 1" /></>,
  notes: <><rect x="3.25" y="3" width="9.5" height="11" rx="1.5" /><path d="M6 2v2h4V2" /><path d="M5.75 7.5h4.5M5.75 10.25h3" /></>,
  compress: <path d="M6 2.5V6H2.5M10 2.5V6h3.5M6 13.5V10H2.5M10 13.5V10h3.5" />,
  thinking: <><path d="M8 2a4.25 4.25 0 0 0-2.4 7.76c.37.26.6.68.6 1.13v.61h3.6v-.61c0-.45.23-.87.6-1.13A4.25 4.25 0 0 0 8 2z" /><path d="M6.5 14h3" /></>,
  wrench: <path d="M10.25 2.5a3.25 3.25 0 0 0-3.03 4.43l-4.3 4.3a1.25 1.25 0 0 0 1.77 1.77l4.3-4.3A3.25 3.25 0 0 0 13.5 5.75l-1.85 1.85-1.9-.35-.35-1.9z" />,
};

interface ToolIconProps {
  kind: ToolIconKind;
  className?: string;
}

export function ToolIcon({ kind, className }: ToolIconProps) {
  return (
    <svg
      className={`jx-toolIcon${className ? ` ${className}` : ''}`}
      viewBox="0 0 16 16"
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[kind]}
    </svg>
  );
}
