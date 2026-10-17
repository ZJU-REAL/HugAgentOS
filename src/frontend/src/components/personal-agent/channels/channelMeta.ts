import type { ChannelType } from '../api';

/** IM platforms that Personal Agent can answer in. Marks are neutral monograms, not brand logos. */
export const CHANNELS: { type: ChannelType; label: string; mark: string; tint: string }[] = [
  { type: 'dingtalk', label: '钉钉', mark: '钉', tint: 'blue' },
  { type: 'lark', label: '飞书', mark: '飞', tint: 'cyan' },
  { type: 'weixin', label: '微信', mark: '微', tint: 'green' },
  { type: 'wecom', label: '企业微信', mark: '企', tint: 'teal' },
];
