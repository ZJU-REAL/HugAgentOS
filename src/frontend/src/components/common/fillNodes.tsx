import { Fragment, type ReactNode } from 'react';

/**
 * 把已翻译句子里的 `{name}` 占位替换成 React 节点（加粗、代码、标签等），
 * 让整句作为一个 i18n key 翻译，而不是拆成碎片拼接。
 * 用法：fillNodes(t("Create in {path}"), { path: <Text code>/v1</Text> })
 */
export function fillNodes(text: string, nodes: Record<string, ReactNode>): ReactNode[] {
  return text.split(/(\{\w+\})/).map((part, i) => {
    const name = /^\{(\w+)\}$/.exec(part)?.[1];
    return <Fragment key={i}>{name !== undefined && name in nodes ? nodes[name] : part}</Fragment>;
  });
}
