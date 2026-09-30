import { t } from '../../i18n';
import type { KbTabKey, MySpaceTab } from '../../types';
export interface NarrowTab {
  key: string;
  label: string;
  /** 'search' 不是页面，点了唤起搜索弹窗 */
  tab: MySpaceTab | 'search';
  scope?: 'personal' | 'team';
  kb?: KbTabKey;
}

export const TABS: NarrowTab[] = [
  { key: 'search', label: t('搜索'), tab: 'search' },
  { key: 'assets-personal', label: t('个人文件夹'), tab: 'assets', scope: 'personal' },
  { key: 'assets-team', label: t('团队文件夹'), tab: 'assets', scope: 'team' },
  { key: 'kb-public', label: t('公共知识库'), tab: 'kb', kb: 'public' },
  { key: 'kb-private', label: t('私有知识库'), tab: 'kb', kb: 'private' },
  { key: 'favorites', label: t('会话收藏'), tab: 'favorites' },
  { key: 'shares', label: t('分享记录'), tab: 'shares' },
  { key: 'notifications', label: t('消息通知'), tab: 'notifications' },
];
