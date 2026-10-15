/**
 * 把新 HTML 就地改到已有 DOM 上，而不是整棵替换 —— 整棵替换会销毁文本节点，
 * 锚在上面的浏览器选区随之作废（流式输出时正文划不中、复制不了）。
 */

export type TextPatch = (previous: Node | null, next: Text) => Node | undefined;

const ELEMENT_NODE = 1;
const TEXT_NODE = 3;
const COMMENT_NODE = 8;

/** React portal 的挂载点：子树归 React 管，新 HTML 里它们是空壳，不能拿去覆盖。 */
function isPortalTarget(el: Element): boolean {
  return el.hasAttribute('data-jxcit') || el.hasAttribute('data-chart');
}

function syncAttributes(oldEl: Element, newEl: Element): void {
  const next = newEl.attributes;
  for (let i = 0; i < next.length; i += 1) {
    const { name, value } = next[i];
    if (oldEl.getAttribute(name) !== value) oldEl.setAttribute(name, value);
  }
  const prev = oldEl.attributes;
  for (let i = prev.length - 1; i >= 0; i -= 1) {
    const { name } = prev[i];
    // Copy feedback belongs to the button, not the next streaming HTML frame.
    if (name === 'data-copy-state' && oldEl.matches('button.jx-mdCode-copy')) continue;
    if (!newEl.hasAttribute(name)) oldEl.removeAttribute(name);
  }
}

function prepareNode(node: Node, textPatch?: TextPatch): Node {
  if (node.nodeType === TEXT_NODE) return textPatch?.(null, node as Text) ?? node;
  if (node.nodeType === ELEMENT_NODE && isPortalTarget(node as Element)) return node;
  for (const child of Array.from(node.childNodes)) {
    const prepared = prepareNode(child, textPatch);
    if (prepared !== child) node.replaceChild(prepared, child);
  }
  return node;
}

function morphNode(parent: Node, oldNode: Node, newNode: Node, textPatch?: TextPatch): void {
  if (newNode.nodeType === TEXT_NODE) {
    const patched = textPatch?.(oldNode, newNode as Text);
    if (patched) {
      if (patched !== oldNode) parent.replaceChild(patched, oldNode);
      return;
    }
  }
  if (oldNode.nodeType !== newNode.nodeType) {
    parent.replaceChild(prepareNode(newNode, textPatch), oldNode);
    return;
  }
  if (oldNode.nodeType === TEXT_NODE || oldNode.nodeType === COMMENT_NODE) {
    if (oldNode.nodeValue !== newNode.nodeValue) oldNode.nodeValue = newNode.nodeValue;
    return;
  }
  if (oldNode.nodeType !== ELEMENT_NODE) {
    parent.replaceChild(prepareNode(newNode, textPatch), oldNode);
    return;
  }
  const oldEl = oldNode as Element;
  const newEl = newNode as Element;
  if (oldEl.tagName !== newEl.tagName) {
    parent.replaceChild(prepareNode(newNode, textPatch), oldNode);
    return;
  }
  syncAttributes(oldEl, newEl);
  if (isPortalTarget(oldEl)) return;
  morphChildren(oldEl, newEl, textPatch);
}

/** 按下标逐个比对子节点：能复用就原地改，多的删、少的补。 */
export function morphChildren(target: Node, source: Node, textPatch?: TextPatch): void {
  const olds = Array.from(target.childNodes);
  const news = Array.from(source.childNodes);
  const shared = Math.min(olds.length, news.length);
  for (let i = 0; i < shared; i += 1) morphNode(target, olds[i], news[i], textPatch);
  for (let i = olds.length - 1; i >= shared; i -= 1) target.removeChild(olds[i]);
  for (let i = shared; i < news.length; i += 1) target.appendChild(prepareNode(news[i], textPatch));
}

export function patchHtml(container: HTMLElement, html: string): void {
  const template = document.createElement('template');
  template.innerHTML = html;
  morphChildren(container, template.content);
}
