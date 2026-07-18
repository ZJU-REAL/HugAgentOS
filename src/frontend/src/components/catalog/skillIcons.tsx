/* eslint-disable react-refresh/only-export-components -- Icon data (PRESETS etc.) lives in the same file as the SkillAvatar component for easy sharing; not a hot-reload-critical path. */
import type { CSSProperties, ReactNode } from 'react';
import { t } from '../../i18n';

// Built-in SVG icon library for skills. Key names must match the value of the backend skill_icon_service.CATEGORY_PRESET.
// Icons are stored as strings: `preset:<key>` / http(s) URL / data-URI. When there is no icon, one is picked via a stable hash of the id,
// guaranteeing every skill has a colorful themed icon; users/admins can override it in the editor.

const SVG = {
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.8,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
};

export interface PresetIcon {
  key: string;
  label: string;
  color: string;
  paths: ReactNode;
}

export const PRESETS: Record<string, PresetIcon> = {
  doc: { key: 'doc', label: t('文档'), color: '#2F6BFF', paths: (<><path d="M6 3h8l4 4v14H6z" /><path d="M14 3v4h4" /><path d="M9 12h6M9 16h6" /></>) },
  pen: { key: 'pen', label: t('写作'), color: '#7C4DFF', paths: (<><path d="M14 4l6 6L9 21H3v-6z" /><path d="M12 6l6 6" /></>) },
  scale: { key: 'scale', label: t('法务'), color: '#00897B', paths: (<><path d="M12 3v18" /><path d="M5 7h14" /><path d="M5 7l-2 6a3 3 0 006 0z" /><path d="M19 7l-2 6a3 3 0 006 0z" /><path d="M8 21h8" /></>) },
  flow: { key: 'flow', label: t('流程'), color: '#1E88E5', paths: (<><rect x="9" y="3" width="6" height="4" rx="1" /><rect x="3" y="17" width="6" height="4" rx="1" /><rect x="15" y="17" width="6" height="4" rx="1" /><path d="M12 7v4M6 17v-2h12v2" /></>) },
  image: { key: 'image', label: t('图片'), color: '#E91E63', paths: (<><rect x="3" y="5" width="18" height="14" rx="2" /><circle cx="8.5" cy="10" r="1.5" /><path d="M21 16l-5-5L5 19" /></>) },
  data: { key: 'data', label: t('数据'), color: '#3949AB', paths: (<><ellipse cx="12" cy="6" rx="7" ry="3" /><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6" /><path d="M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3" /></>) },
  policy: { key: 'policy', label: t('政务'), color: '#00838F', paths: (<><path d="M3 9l9-5 9 5" /><path d="M5 9v9M9 9v9M15 9v9M19 9v9" /><path d="M3 21h18" /></>) },
  code: { key: 'code', label: t('代码'), color: '#455A64', paths: (<><path d="M8 8l-4 4 4 4M16 8l4 4-4 4M13 5l-2 14" /></>) },
  megaphone: { key: 'megaphone', label: t('营销'), color: '#F4511E', paths: (<><path d="M4 10v4h3l6 4V6L7 10H4z" /><path d="M17 9a4 4 0 010 6" /></>) },
  chart: { key: 'chart', label: t('图表'), color: '#2E7D32', paths: (<><path d="M3 20h18" /><line x1="6" y1="20" x2="6" y2="12" /><line x1="11" y1="20" x2="11" y2="5" /><line x1="16" y1="20" x2="16" y2="9" /></>) },
  finance: { key: 'finance', label: t('财务'), color: '#C62828', paths: (<><circle cx="12" cy="12" r="9" /><path d="M8 8l4 5 4-5M12 13v5M9 14h6M9 11.5h6" /></>) },
  book: { key: 'book', label: t('知识'), color: '#6D4C41', paths: (<><path d="M5 4a1 1 0 011-1h13v16H6a1 1 0 00-1 1z" /><path d="M5 20a1 1 0 011-1h13" /></>) },
  target: { key: 'target', label: t('策略'), color: '#D81B60', paths: (<><circle cx="12" cy="12" r="8" /><circle cx="12" cy="12" r="4" /><circle cx="12" cy="12" r="1" /></>) },
  shield: { key: 'shield', label: t('安全'), color: '#388E3C', paths: (<><path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z" /><path d="M9 12l2 2 4-4" /></>) },
  mindmap: { key: 'mindmap', label: t('脑图'), color: '#5E35B1', paths: (<><circle cx="4" cy="12" r="2" /><circle cx="20" cy="7" r="2" /><circle cx="20" cy="17" r="2" /><path d="M6 12h6M12 12l6-4M12 12l6 4" /></>) },
  search: { key: 'search', label: t('检索'), color: '#546E7A', paths: (<><circle cx="11" cy="11" r="7" /><path d="M21 21l-5-5" /></>) },
  robot: { key: 'robot', label: t('智能'), color: '#00ACC1', paths: (<><rect x="4" y="8" width="16" height="12" rx="2" /><path d="M12 8V4M9 4h6" /><circle cx="9" cy="13" r="1" /><circle cx="15" cy="13" r="1" /><path d="M9.5 17h5" /></>) },
  idea: { key: 'idea', label: t('创意'), color: '#F9A825', paths: (<><path d="M9 18h6M10 21h4" /><path d="M12 3a6 6 0 00-4 10c.7.7 1 1.5 1 2h6c0-.5.3-1.3 1-2a6 6 0 00-4-10z" /></>) },
  calendar: { key: 'calendar', label: t('日程'), color: '#5C6BC0', paths: (<><rect x="3" y="5" width="18" height="16" rx="2" /><path d="M3 9h18M8 3v4M16 3v4" /></>) },
  mail: { key: 'mail', label: t('邮件'), color: '#26A69A', paths: (<><rect x="3" y="5" width="18" height="14" rx="2" /><path d="M3 7l9 6 9-6" /></>) },
  chat: { key: 'chat', label: t('对话'), color: '#42A5F5', paths: (<><path d="M4 5h16a1 1 0 011 1v9a1 1 0 01-1 1H9l-4 4v-4H4a1 1 0 01-1-1V6a1 1 0 011-1z" /><path d="M8 9h8M8 12h5" /></>) },
  globe: { key: 'globe', label: t('网络'), color: '#29B6F6', paths: (<><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18" /></>) },
  gear: { key: 'gear', label: t('设置'), color: '#78909C', paths: (<><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2" /></>) },
  clock: { key: 'clock', label: t('时间'), color: '#8D6E63', paths: (<><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>) },
  tag: { key: 'tag', label: t('标签'), color: '#AB47BC', paths: (<><path d="M3 12V4h8l9 9-7 7-9-9z" /><circle cx="7.5" cy="7.5" r="1.2" /></>) },
  folder: { key: 'folder', label: t('文件夹'), color: '#FFA726', paths: (<><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z" /></>) },
  cloud: { key: 'cloud', label: t('云端'), color: '#4FC3F7', paths: (<><path d="M7 18a4 4 0 01-.5-8 6 6 0 0111.5 1.5A3.5 3.5 0 0117 18z" /></>) },
  lock: { key: 'lock', label: t('加密'), color: '#EF5350', paths: (<><rect x="5" y="11" width="14" height="9" rx="2" /><path d="M8 11V8a4 4 0 018 0v3" /></>) },
  users: { key: 'users', label: t('团队'), color: '#66BB6A', paths: (<><circle cx="9" cy="9" r="3" /><circle cx="17" cy="10" r="2" /><path d="M3 19a6 6 0 0112 0M15 19a4 4 0 016-3" /></>) },
  location: { key: 'location', label: t('位置'), color: '#EC407A', paths: (<><path d="M12 21s7-6 7-11a7 7 0 10-14 0c0 5 7 11 7 11z" /><circle cx="12" cy="10" r="2.5" /></>) },
  bolt: { key: 'bolt', label: t('自动化'), color: '#FBC02D', paths: (<><path d="M13 3L5 13h6l-2 8 8-10h-6z" /></>) },
  star: { key: 'star', label: t('收藏'), color: '#FFB300', paths: (<><path d="M12 3l2.7 5.5 6 .9-4.3 4.2 1 6-5.4-2.8L6.6 19.6l1-6L3.3 9.4l6-.9z" /></>) },
  checklist: { key: 'checklist', label: t('清单'), color: '#43A047', paths: (<><path d="M4 7l2 2 3-3M4 17l2 2 3-3M13 7h7M13 17h7" /></>) },
  translate: { key: 'translate', label: t('翻译'), color: '#5E97F6', paths: (<><path d="M4 5h8M8 5v2c0 3-2 6-5 7M6 9c0 2 2 4 5 5M13 19l4-9 4 9M14.5 16h5" /></>) },
  table: { key: 'table', label: t('表格'), color: '#3F51B5', paths: (<><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M3 10h18M9 4v16" /></>) },
  calculator: { key: 'calculator', label: t('计算'), color: '#009688', paths: (<><rect x="5" y="3" width="14" height="18" rx="2" /><path d="M8 7h8M8 12h.01M12 12h.01M16 12h.01M8 16h.01M12 16h.01M16 16h.01" /></>) },
  rocket: { key: 'rocket', label: t('增长'), color: '#FF7043', paths: (<><path d="M12 3c3 1 5 4 5 8l-3 3H10L7 11c0-4 2-7 5-8z" /><path d="M9 16l-2 4M15 16l2 4" /><circle cx="12" cy="10" r="1.4" /></>) },
  flag: { key: 'flag', label: t('里程碑'), color: '#C0392B', paths: (<><path d="M5 21V4M5 4h11l-2 4 2 4H5" /></>) },
  terminal: { key: 'terminal', label: t('命令行'), color: '#37474F', paths: (<><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M7 9l3 3-3 3M13 15h4" /></>) },
  link: { key: 'link', label: t('链接'), color: '#00ACC1', paths: (<><path d="M9 15l6-6" /><path d="M10 6l1-1a4 4 0 015 5l-1 1M14 18l-1 1a4 4 0 01-5-5l1-1" /></>) },
  shield_check: { key: 'shield_check', label: t('合规'), color: '#1E88E5', paths: (<><path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z" /><path d="M9 11l2 2 4-4" /></>) },
  bell: { key: 'bell', label: t('通知'), color: '#F57C00', paths: (<><path d="M6 9a6 6 0 0112 0c0 5 2 6 2 6H4s2-1 2-6z" /><path d="M10 20a2 2 0 004 0" /></>) },
};

export const PRESET_LIST: PresetIcon[] = Object.values(PRESETS);

// Category → preset key (mirrors the backend, used as a fallback for marketplace cards when there is no icon_url).
const CATEGORY_PRESET: Record<string, string> = {
  // The current 8 marketplace categories
  写作助手: 'pen', 文档处理: 'doc', 数据分析: 'chart', 政策产业: 'policy',
  营销创意: 'megaphone', 法务合规: 'scale', 办公效率: 'flow', 研发效率: 'code', 社区共享: 'book',
  // Legacy category names (compatibility with installed skills / old categories self-entered in community submissions)
  公文写作: 'doc', 写作润色: 'pen', 可视化绘图: 'flow', 创意设计: 'image',
  流程效率: 'flow', 数据查询: 'data', 政策服务: 'policy',
  营销策划: 'megaphone', 财务分析: 'finance', 知识管理: 'book',
  商业策略: 'target', 数据安全: 'shield', 产业分析: 'chart', 政务服务: 'policy',
  翻译语言: 'doc', 项目管理: 'flow',
};

export function categoryPreset(category?: string): string {
  return 'preset:' + (CATEGORY_PRESET[(category || '').trim()] || 'doc');
}

function hashStr(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}

function tint(hex: string): string {
  // Convert the accent color into a light background at 12% opacity
  const n = hex.replace('#', '');
  const r = parseInt(n.slice(0, 2), 16), g = parseInt(n.slice(2, 4), 16), b = parseInt(n.slice(4, 6), 16);
  return `rgba(${r},${g},${b},0.12)`;
}

function resolvePresetKey(icon: string | undefined, seed: string): string {
  if (icon && icon.startsWith('preset:')) {
    const k = icon.slice(7);
    if (PRESETS[k]) return k;
  }
  const keys = Object.keys(PRESETS);
  return keys[hashStr(seed || '?') % keys.length];
}

/** Skill avatar: prefer a url/data-URI image; otherwise use a preset (explicit or via a stable hash of the seed). */
export function SkillAvatar({
  icon, name, seed, size = 40, radius, round,
}: { icon?: string; name?: string; seed?: string; size?: number; radius?: number; round?: boolean }) {
  const box: CSSProperties = {
    width: size, height: size, flex: `0 0 ${size}px`,
    borderRadius: round ? '50%' : (radius ?? Math.round(size * 0.24)),
    display: 'flex', alignItems: 'center', justifyContent: 'center', overflow: 'hidden',
  };
  if (icon && (icon.startsWith('http') || icon.startsWith('data:'))) {
    return <img className="jx-skillAvatar" style={{ ...box, objectFit: 'cover' }} src={icon} alt="" loading="lazy" />;
  }
  const p = PRESETS[resolvePresetKey(icon, seed || name || '?')];
  return (
    <div className="jx-skillAvatar" style={{ ...box, background: tint(p.color), color: p.color }}>
      <svg {...SVG} width={Math.round(size * 0.56)} height={Math.round(size * 0.56)}>{p.paths}</svg>
    </div>
  );
}
