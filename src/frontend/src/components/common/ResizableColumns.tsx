/**
 * Drag-to-resize table columns —— zero-dependency implementation.
 *
 * Background: antd v6's <Table> has no built-in column-width dragging; the
 * community's common react-resizable depends on react-draggable's findDOMNode,
 * which was removed in React 19, so here we implement it with native pointer
 * events —— no extra dependencies, React 19 safe.
 *
 * Usage (any config/admin table):
 *   const { columns, components, tableProps } = useResizableColumns(baseColumns, { storageKey: 'tool-logs' });
 *   <Table columns={columns} components={components} {...tableProps} ... />
 *
 * - Dragging is enabled only for columns that "have a numeric width and are not fixed" (action columns and other fixed columns stay as-is).
 * - Drag results are persisted to localStorage by storageKey, keeping user adjustments across refreshes.
 * - tableProps carries tableLayout:'fixed', ensuring column widths take effect immediately after dragging (under auto layout, width is only a suggestion).
 */
import { useCallback, useMemo, useRef, useState } from 'react';

const MIN_WIDTH = 48;
const LS_PREFIX = 'jx.colw.';

function loadWidths(storageKey?: string): Record<string, number> {
  if (!storageKey) return {};
  try {
    const raw = localStorage.getItem(LS_PREFIX + storageKey);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function saveWidths(storageKey: string | undefined, widths: Record<string, number>): void {
  if (!storageKey) return;
  try {
    localStorage.setItem(LS_PREFIX + storageKey, JSON.stringify(widths));
  } catch {
    /* Quota full / private mode: ignore, dragging still takes effect within this session */
  }
}

interface HeaderCellProps {
  width?: number;
  resizable?: boolean;
  onResize?: (next: number) => void;
  children?: React.ReactNode;
  style?: React.CSSProperties;
  [key: string]: unknown;
}

/** Custom header cell: place a col-resize handle at the right edge; once pressed, follow the pointer to change column width. */
function ResizableHeaderCell(props: HeaderCellProps) {
  const { width, resizable, onResize, children, style, ...rest } = props;
  const thRef = useRef<HTMLTableCellElement>(null);

  const onPointerDown = useCallback(
    (e: React.PointerEvent) => {
      if (!onResize) return;
      e.preventDefault();
      e.stopPropagation();
      const startX = e.clientX;
      const startW = thRef.current?.offsetWidth ?? width ?? MIN_WIDTH;
      const move = (ev: PointerEvent) => {
        onResize(Math.max(MIN_WIDTH, Math.round(startW + (ev.clientX - startX))));
      };
      const up = () => {
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', up);
        document.body.style.userSelect = '';
        document.body.style.cursor = '';
      };
      document.body.style.userSelect = 'none';
      document.body.style.cursor = 'col-resize';
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', up);
    },
    [onResize, width],
  );

  if (!resizable) {
    return <th {...rest} style={style}>{children}</th>;
  }

  return (
    <th {...rest} ref={thRef} style={{ ...style, position: 'relative' }}>
      {children}
      <span
        className="jx-col-resize-handle"
        onPointerDown={onPointerDown}
        // A click on the handle should not bubble up and trigger column sorting
        onClick={(e) => e.stopPropagation()}
      />
    </th>
  );
}

const RESIZABLE_COMPONENTS = { header: { cell: ResizableHeaderCell } };

type AnyColumn = Record<string, unknown> & {
  key?: React.Key;
  dataIndex?: React.Key | React.Key[];
  width?: number | string;
  fixed?: unknown;
};

export interface UseResizableColumnsResult<C> {
  columns: C[];
  components: typeof RESIZABLE_COMPONENTS;
  /** Extra props passed through to <Table> (currently only tableLayout:'fixed', ensuring dragging takes effect immediately). */
  tableProps: { tableLayout: 'fixed' };
}

/**
 * Wrap a set of column definitions into "drag-to-resize". Returns the new columns
 * (with onHeaderCell injected), components (custom header cell), and tableProps.
 */
export function useResizableColumns<C extends AnyColumn>(
  baseColumns: C[],
  opts?: { storageKey?: string },
): UseResizableColumnsResult<C> {
  const storageKey = opts?.storageKey;
  const [widths, setWidths] = useState<Record<string, number>>(() => loadWidths(storageKey));

  const handleResize = useCallback(
    (colKey: string) => (next: number) => {
      setWidths((prev) => {
        const merged = { ...prev, [colKey]: next };
        saveWidths(storageKey, merged);
        return merged;
      });
    },
    [storageKey],
  );

  const columns = useMemo(
    () =>
      baseColumns.map((col, idx) => {
        const colKey = String(col.key ?? (Array.isArray(col.dataIndex) ? col.dataIndex.join('.') : col.dataIndex) ?? idx);
        const baseWidth = col.width;
        const effectiveWidth = widths[colKey] ?? baseWidth;
        // Resizable only when the column itself has a numeric width and is not fixed
        const resizable = typeof effectiveWidth === 'number' && !col.fixed;
        return {
          ...col,
          width: effectiveWidth,
          onHeaderCell: () => ({
            width: typeof effectiveWidth === 'number' ? effectiveWidth : undefined,
            resizable,
            onResize: resizable ? handleResize(colKey) : undefined,
          }),
        } as C;
      }),
    [baseColumns, widths, handleResize],
  );

  return { columns, components: RESIZABLE_COMPONENTS, tableProps: { tableLayout: 'fixed' } };
}
