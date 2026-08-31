import { createContext, useContext } from 'react';

/**
 * 工具卡所属的「哪条消息」。
 *
 * 历史里的工具结果只下发梗概，展开时才回后端取全文（见 `getToolCallResult`）——
 * 取的时候要报出 chat_id + message_id + tool_id。工具卡外面套着 ToolRunShell /
 * ToolProgressInline 好几层，逐层透传三个参数会把这几层的签名都搅浑，所以用一个
 * 只读上下文在 MessageBubble 处一次性挂上。
 *
 * 值为 null（流式过程中、批量面板等没有落库消息的场景）时，工具卡就老老实实只显示
 * 手上这份内容，不去取全文——那时候本来也还没有可取的持久化副本。
 */
export interface ToolMessageIdentity {
  chatId: string;
  /** 消息尚未落库（正在流式输出）时为空。 */
  messageId?: string;
}

export const ToolMessageContext = createContext<ToolMessageIdentity | null>(null);

export function useToolMessageIdentity(): ToolMessageIdentity | null {
  return useContext(ToolMessageContext);
}
