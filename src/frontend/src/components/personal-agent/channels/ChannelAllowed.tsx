import { useState } from 'react';
import { Button, Input, Modal, message } from 'antd';
import { personalApi, errorText, type ChannelAccount, type PersonalChannel } from '../api';
import { t } from '../../../i18n';

interface ChannelAllowedProps {
  bot: PersonalChannel;
  onChange: (bots: PersonalChannel[]) => void;
  /** Reload the bots, so a new private sender shows up. */
  onRefresh: () => void;
}

const label = (account: ChannelAccount) => account.sender_name ? account.sender_name + ' · ' + account.sender_id : account.sender_id;
const when = (seconds?: number) => seconds ? new Date(seconds * 1000).toLocaleString() : '';

/** The IM accounts allowed to instruct one bot: listed from recent private senders or by ID. */
export function ChannelAllowed({ bot, onChange, onRefresh }: ChannelAllowedProps) {
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState('');
  const access = bot.access;
  if (!access) return null;

  async function allow(body: { key?: string; sender_id?: string }) {
    setBusy(body.key || 'typed');
    try { onChange(await personalApi.allowChannelAccount(bot.channel_id, body)); if (!body.key) setTyped(''); }
    catch (exc) { message.error(t('加入白名单失败：{reason}', { reason: errorText(exc) })); }
    finally { setBusy(''); }
  }
  function remove(account: ChannelAccount) {
    Modal.confirm({
      title: t('从白名单移除 {name}？', { name: label(account) }),
      content: t('移除后，这个账号私聊机器人不再有回应，在群聊里说的话也只作为背景。'),
      okText: t('移除'), okButtonProps: { danger: true }, cancelText: t('取消'),
      onOk: async () => {
        try { onChange(await personalApi.removeChannelAccount(bot.channel_id, account.key)); }
        catch (exc) { message.error(t('移除失败：{reason}', { reason: errorText(exc) })); }
      },
    });
  }
  return <div className="pa-channel-owner">
    <p className="pa-hint" role="note">{access.allowed.length
      ? t('只有白名单里的账号能私聊或在群聊中指挥机器人，其他人的群消息只作为背景。')
      : t('白名单还是空的：机器人不会执行任何人的指令。先用你自己的账号私聊机器人一句话，再在下面把它加入白名单。')}</p>
    {access.allowed.length > 0 && <div className="pa-channel-recent">
      <strong>{t('白名单')}</strong>
      <ul className="pa-channel-accounts">{access.allowed.map(account => <li key={account.key}>
        <span>{label(account)}</span>
        <Button size="small" type="text" danger onClick={() => remove(account)}>{t('移除')}</Button>
      </li>)}</ul>
    </div>}
    <div className="pa-channel-recent">
      <div className="pa-channel-recent-head">
        <strong>{t('最近私聊过机器人的账号')}</strong>
        <Button size="small" type="link" onClick={onRefresh}>{t('刷新')}</Button>
      </div>
      {access.recent.length
        ? <ul className="pa-channel-accounts">{access.recent.map(account => <li key={account.key}>
            <span>{label(account)}<small className="pa-hint">{when(account.last_at)}</small></span>
            <Button size="small" loading={busy === account.key} onClick={() => void allow({ key: account.key })}>{t('加入白名单')}</Button>
          </li>)}</ul>
        : <p className="pa-hint">{t('还没有。用你自己的账号私聊机器人后，点「刷新」。')}</p>}
      <p className="pa-hint">{t('名字可以随便改，请对照账号 ID 和时间确认是你自己。')}</p>
    </div>
    <div className="pa-channel-owner-actions">
      <Input size="small" value={typed} onChange={event => setTyped(event.target.value)} placeholder={t('或直接填写账号 ID')} aria-label={t('账号 ID')} />
      <Button size="small" disabled={!typed.trim()} loading={busy === 'typed'} onClick={() => void allow({ sender_id: typed.trim() })}>{t('添加')}</Button>
    </div>
  </div>;
}
