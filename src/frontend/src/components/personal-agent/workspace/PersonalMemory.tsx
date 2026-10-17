import { Alert, Button, Tag } from 'antd';
import { DatabaseOutlined, FileTextOutlined, UserOutlined } from '@ant-design/icons';
import { SettingGroup, SettingRow } from '../settings/SettingRow';
import { navigateTo, pathForPanel } from '../../../routing/navigation';
import { t } from '../../../i18n';
import { PersonalDreaming } from './PersonalDreaming';

/** Personal Agent file memory is independent of the ordinary-chat memory settings. */
export function PersonalMemory({ onClose, status }: { onClose: () => void; status?: { available: boolean; pending: number; retrying: number } }) {
  return <SettingGroup title={t('记忆')}>
    {status && (!status.available || status.retrying > 0) && <Alert type="warning" showIcon
      title={t('部分记忆等待写入，工作区恢复可用后会自动重试。')} />}
    {status && status.pending > 0 && <span role="status">{t('待写入日志：{count} 条', { count: status.pending })}</span>}
    <SettingRow icon={<DatabaseOutlined />} label={t('长期记忆 · MEMORY.md')}
      description={t('保存长期事实、决定和经验；智能体按需更新，下次对话和任务会读取。')}
      control={<Tag bordered={false} color="green">{t(status?.available ? '已启用' : '文件记忆')}</Tag>} />
    <SettingRow icon={<UserOutlined />} label={t('用户偏好 · USER.md')}
      description={t('保存你的稳定偏好和个人资料，可由你或智能体修改。')}
      control={<Tag bordered={false}>{t('文件记忆')}</Tag>} />
    <SettingRow icon={<FileTextOutlined />} label={t('每日日志 · memory/YYYY-MM-DD.md')}
      description={t('对话和任务结束后自动记录，按你的时区归档；写入失败会重试。')}
      control={<Button size="small" onClick={() => { onClose(); navigateTo(pathForPanel('my_space', 'agent')); }}>{t('查看和编辑记忆')}</Button>} />
    <PersonalDreaming />
  </SettingGroup>;
}
