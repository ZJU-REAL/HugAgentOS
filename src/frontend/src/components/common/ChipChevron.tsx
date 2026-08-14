/**
 * 输入框工具条 chip 的统一收起/展开箭头。
 *
 * 用 inline SVG 而不是 `<img src="/home/arrow-down.svg">`：颜色跟 currentColor 走，
 * 选中态 / hover 态换色不用再叠 filter hack；展开时由父级 `.open` 触发 180° 旋转。
 */
export function ChipChevron({ className = '' }: { className?: string }) {
  return (
    <svg
      className={`jx-modeArrow${className ? ` ${className}` : ''}`}
      width="12"
      height="12"
      viewBox="0 0 12 12"
      fill="none"
      aria-hidden="true"
      focusable="false"
    >
      <path
        d="M3 4.5L6 7.5L9 4.5"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export default ChipChevron;
