import { useEffect, useMemo, useState } from 'react';
import { message } from 'antd';
import { getWeixinBindStatus, prepareChannelLocalBinding, startWeixinBind } from '../api';
import { t } from '../i18n';
import {
  createWeixinBindingFlow, INITIAL_WEIXIN_BINDING, type WeixinBindingState,
} from '../components/settings/weixinBindingFlow';

type BindingFlow = ReturnType<typeof createWeixinBindingFlow>;

export function useWeixinBinding(
  agentId: string | undefined, executionLocation: 'cloud' | 'local',
  localReady: boolean, refresh: () => Promise<void>,
) {
  const [snapshot, setSnapshot] = useState<{ flow: BindingFlow | null; state: WeixinBindingState }>({
    flow: null, state: INITIAL_WEIXIN_BINDING,
  });
  const flow = useMemo(() => {
    const bindingFlow: BindingFlow = createWeixinBindingFlow({
      start: async () => {
        const binding = executionLocation === 'local' ? await prepareChannelLocalBinding() : undefined;
        return startWeixinBind(agentId, binding?.binding_id);
      },
      poll: getWeixinBindStatus,
      onChange: state => setSnapshot({ flow: bindingFlow, state }),
      onConfirmed: async () => {
        message.success(t('微信已绑定'));
        await refresh();
      },
    });
    return bindingFlow;
  }, [agentId, executionLocation, refresh]);
  useEffect(() => () => flow.dispose(), [flow]);
  const start = async () => {
    if (executionLocation === 'local' && !localReady) {
      setSnapshot({ flow, state: {
        open: true, phase: 'error', image: '', tip: t('本机服务尚未就绪，暂不可选择本机。'),
      } });
      return;
    }
    await flow.start();
  };
  // Changing agent/location invalidates the old UI immediately, before effect cleanup.
  const state = snapshot.flow === flow ? snapshot.state : INITIAL_WEIXIN_BINDING;
  return { ...state, start, close: flow.close };
}
