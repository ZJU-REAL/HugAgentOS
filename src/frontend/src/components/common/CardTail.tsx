import type { ReactNode } from 'react';
import { Switch } from 'antd';

/**
 * 能力卡片右上角尾部：常驻的启停胶囊开关 + 悬浮/聚焦才显形的操作按钮。
 * 技能、智能体、连接器（MCP）、插件四类卡片共用，保证启停一律在详情页外完成。
 */
export function CardTail({ checked, onChange, actions }: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  actions?: ReactNode;
}) {
  // 容器级 stopPropagation：Popconfirm 的确认按钮渲染在 portal 里，但 React 合成事件仍沿
  // 组件树冒泡，不在这里拦住就会冒到卡片 onClick —— 点「确认删除」反而跳进详情页。
  return (
    <div className="jx-cardTail" onClick={(e) => e.stopPropagation()}>
      {actions ? <span className="jx-cardTailActions">{actions}</span> : null}
      <Switch className="jx-cardSwitch" checked={checked} onChange={onChange} />
    </div>
  );
}
