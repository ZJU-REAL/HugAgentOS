import { Button } from 'antd';
import { useChatModeStore, useChatStore } from '../../stores';
import { t } from '../../i18n';
import {
  alternativeModes, intentQuestion, previousModeName, type IntentRouteResult,
} from '../../utils/intentRoute';
import { useToolMessageIdentity } from './ToolMessageContext';

interface IntentRecognitionBodyProps {
  route: IntentRouteResult | null;
  /** tool_args：`{ question }` */
  input: unknown;
}

/**
 * 「用户意图识别」工具卡展开后的内容：问题、结果、理由，以及「换成」其他候选模式重答。
 *
 * 结果一行说清进了哪个模式；`switched` 时补上从哪个模式切过来，`source=fallback`
 * 时直说识别没完成、用的是哪个。换模式会把这段对话的模式位改成选中的模式（退出智能
 * 路由），再走现有的重新生成链路——见 MessageBubble 挂上的 `rerunWithMode`。
 */
export function IntentRecognitionBody({ route, input }: IntentRecognitionBodyProps) {
  const identity = useToolMessageIdentity();
  const sending = useChatStore((s) => s.sending);
  // Without mode switching every rerun lands in the default mode anyway: offer no switch.
  const canSwitch = useChatModeStore((st) => st.canSwitch);
  const question = intentQuestion(input);

  if (!route) {
    return <div className="jx-tr-empty">{t('没有拿到识别结果')}</div>;
  }

  // source=fallback 是约定的标志；fallback_cause 非空（timeout / parse_error …）同样说明没识别成功
  const fallback = route.source === 'fallback' || !!route.fallback_cause;
  const previous = route.switched ? previousModeName(route) : '';
  const result = fallback
    ? t('识别未完成，已使用「{mode}」', { mode: route.mode_name })
    : previous
      ? t('进入「{mode}」（从「{from}」切换）', { mode: route.mode_name, from: previous })
      : t('进入「{mode}」', { mode: route.mode_name });
  const alternatives = alternativeModes(route);
  const rerun = canSwitch ? identity?.rerunWithMode : undefined;

  return (
    <div className="jx-intentRoute">
      {question && (
        <div className="jx-intentRoute-row">
          <span className="jx-intentRoute-key">{t('问题')}</span>
          <span className="jx-intentRoute-val">{question}</span>
        </div>
      )}
      <div className="jx-intentRoute-row">
        <span className="jx-intentRoute-key">{t('结果')}</span>
        <span className="jx-intentRoute-val">{result}</span>
      </div>
      {route.reason && (
        <div className="jx-intentRoute-row">
          <span className="jx-intentRoute-key">{t('理由')}</span>
          <span className="jx-intentRoute-val">{route.reason}</span>
        </div>
      )}
      {rerun && alternatives.length > 0 && (
        <div className="jx-intentRoute-row jx-intentRoute-switch">
          <span className="jx-intentRoute-key">{t('换成')}</span>
          <span className="jx-intentRoute-options">
            {alternatives.map((mode, index) => (
              <span key={mode.slug} className="jx-intentRoute-option">
                {index > 0 && <span className="jx-intentRoute-sep" aria-hidden="true">/</span>}
                <Button
                  type="link"
                  size="small"
                  className="jx-intentRoute-optionBtn"
                  disabled={sending}
                  title={t('用「{mode}」重新回答这一轮', { mode: mode.name })}
                  onClick={() => rerun(mode.slug)}
                >
                  {mode.name}
                </Button>
              </span>
            ))}
          </span>
        </div>
      )}
    </div>
  );
}
