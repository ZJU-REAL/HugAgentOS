import type { UserAgentItem } from '../../stores/agentStore';

const AGENT_ICON_MAP: Record<string, string> = {
  '报告生成智能体': '/home/agent-icons/report.svg',
  '知识检索智能体': '/home/agent-icons/knowledge.svg',
  // 历史名称（改称「智能体」前入库的存量数据）仍要能查到图标
  '报告生成子智能体': '/home/agent-icons/report.svg',
  '知识检索子智能体': '/home/agent-icons/knowledge.svg',
  '报告撰写': '/home/agent-icons/report-writing.svg',
  '知识检索': '/home/agent-icons/knowledge-search.svg',
  '智能问答': '/home/agent-icons/qa.svg',
  '数据分析': '/home/agent-icons/data-analysis.svg',
  '政策解读': '/home/agent-icons/policy.svg',
  '信息提取': '/home/agent-icons/info-extract.svg',
  '材料分析': '/home/agent-icons/material-analysis.svg',
  '流程指引': '/home/agent-icons/process-guide.svg',
};

const RANDOM_ICONS = [
  'Frame 442.svg', 'Frame 443.svg', 'Frame 444.svg', 'Frame 445.svg',
  'Frame 446.svg', 'Frame 447.svg', 'Frame 448.svg', 'Frame 449.svg',
  'Frame 450.svg', 'Frame 451.svg', 'Frame 452.svg', 'Frame 453.svg',
  'Frame 454.svg', 'Frame 455.svg', 'Frame 456.svg', 'Frame 457.svg',
  'Frame 458.svg', 'Frame 459.svg', 'Frame 460.svg', 'Frame 461.svg',
  'Frame 462.svg', 'Frame 463.svg', 'Frame 464.svg', 'Frame 465.svg',
  'Frame 466.svg', 'Frame 467.svg', 'Frame 468.svg', 'Frame 469.svg',
  'Frame 470.svg', 'Frame 471.svg', 'Frame 472.svg',
];

/** Deterministic hash from string → index into RANDOM_ICONS. */
function hashToIconIndex(str: string): number {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    hash = ((hash << 5) - hash + str.charCodeAt(i)) | 0;
  }
  return Math.abs(hash) % RANDOM_ICONS.length;
}

/** Get a stable fallback icon URL based on agent id or name. */
export function getRandomIconUrl(key: string): string {
  const fileName = RANDOM_ICONS[hashToIconIndex(key)];
  return `/home/random-icons/${encodeURIComponent(fileName)}`;
}

// The avatar may be an image address (/path, http(s), data:, blob:) or emoji/text.
function isImageAvatar(src: string): boolean {
  return /^(https?:|data:|blob:|\/)/.test(src.trim());
}

/** Shared agent avatar used by the ability center and invocation menus. */
export function AgentIcon({ agent, size }: { agent: UserAgentItem; size: number; colorIndex?: number }) {
  const radius = size < 36 ? '50%' : 8;
  const avatar = agent.avatar?.trim();
  if (avatar) {
    if (isImageAvatar(avatar)) {
      return <img src={avatar} alt="" width={size} height={size}
        style={{ borderRadius: radius, objectFit: 'cover', display: 'block' }} />;
    }
    return (
      <span style={{
        width: size, height: size, borderRadius: radius,
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        fontSize: Math.round(size * 0.62), lineHeight: 1,
        background: 'var(--color-primary-light, #EBF2FF)',
      }}>{avatar}</span>
    );
  }
  const mapped = AGENT_ICON_MAP[agent.name];
  if (mapped) {
    return <img src={mapped} alt="" width={size} height={size}
      style={{ borderRadius: radius, objectFit: 'cover', display: 'block' }} />;
  }
  return <img src={getRandomIconUrl(agent.agent_id || agent.name)} alt="" width={size} height={size}
    style={{ borderRadius: radius, objectFit: 'cover', display: 'block' }} />;
}
