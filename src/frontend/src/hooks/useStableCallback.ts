import { useCallback, useLayoutEffect, useRef } from 'react';

/**
 * 把一个每次渲染都新建的回调包成**身份恒定**的那一个。
 *
 * 为什么需要：消息气泡要能 `memo` 掉，前提是传进去的 props 在内容没变时身份也不变。
 * 但 `send` / `regenerate` / `editAndResend` / `exportChatRecord` 都是父组件里
 * 每次渲染重新定义的普通函数——只要它们的身份每帧都在变，`memo` 就形同虚设，
 * 模型每吐一个字，整条对话的所有气泡还是要跟着重渲染一遍。
 *
 * 包一层之后：对外始终是同一个函数对象，内部每次渲染把最新实现存进 ref，
 * 调用时转发过去，所以既不会读到过期闭包，也不会破坏 `memo`。
 *
 * `undefined` 会原样透传——`regenerate` / `editAndResend` 在调用处是被当作
 * 「有没有这个能力」的开关用的（`{regenerate && <button/>}`），不能凭空变出一个函数。
 */
export function useStableCallback<T extends (...args: never[]) => unknown>(fn: T): T;
export function useStableCallback<T extends (...args: never[]) => unknown>(
  fn: T | undefined,
): T | undefined;
export function useStableCallback<T extends (...args: never[]) => unknown>(
  fn: T | undefined,
): T | undefined {
  const latest = useRef(fn);
  useLayoutEffect(() => {
    latest.current = fn;
  });
  // 空依赖：这个包装函数从挂载到卸载都是同一个对象，转发时才去读最新实现，
  // 所以既稳定又不会读到过期闭包。
  const stable = useCallback((...args: never[]) => latest.current?.(...args), []);
  return fn ? (stable as unknown as T) : undefined;
}

export default useStableCallback;
