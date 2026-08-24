import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { CheckOutlined, CopyOutlined } from '@ant-design/icons';
import { findCitedRange } from '../../../utils/highlight';
import { WindowedText } from '../../common/WindowedText';
import { t } from '../../../i18n';

/**
 * 工具结果的**可读**视图 —— 取代原来那块「定高 <pre> + 裸 JSON.stringify」。
 *
 * 三条设计约束，都是实际用下来提的：
 *  1. 不要满屏花括号。字段渲染成「标签 + 值」，列表渲染成带序号的卡片，
 *     JSON 原文降级为顶栏的一个开关（要对字段名、要复制时才用）。
 *  2. 不要一次铺开。长列表按 12 条一批出，剩下的留「显示更多」；长文本先夹住，
 *     想看再展开——省得一个结果糊出上千张卡片。
 *  3. 从引用点开时只给**证据附近**。命中片段所在的那条记录整条展示（比原来那
 *     500 字截断多得多），其余部分收在「查看完整结果」后面。
 */

interface DataViewProps {
  /** 已解析的结果，或原始字符串（会尝试 parse，失败按纯文本展示） */
  value: unknown;
  /** 被引用的证据片段：定位后只展示命中记录，并在文本里高亮 */
  focus?: string;
  /** 折叠态最大高度（px） */
  maxHeight?: number;
  className?: string;
}

type Path = Array<string | number>;

/** 每批渲染多少条列表项 */
const PAGE = 12;
/** 超过这个长度的字符串另起一段展示，不再挤在标签右边 */
const LONG_TEXT = 120;
/** 默认展开的层级 */
const OPEN_DEPTH = 2;

const isPlainObject = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === 'object' && !Array.isArray(v);

const TITLE_KEYS = ['title', '标题', '文件名称', '企业名称', '产品名称', 'name', '名称', 'document_name', 'file_name'];
const URL_RE = /^https?:\/\/\S+$/i;

function firstTitle(obj: Record<string, unknown>): { key: string; text: string } | null {
  for (const k of TITLE_KEYS) {
    const v = obj[k];
    if ((typeof v === 'string' || typeof v === 'number') && String(v).trim()) {
      return { key: k, text: String(v) };
    }
  }
  return null;
}

/** 深搜第一处包含引用片段的字符串值，返回它的路径 */
function findFocusPath(root: unknown, fragment: string): Path | null {
  if (!fragment) return null;
  let found: Path | null = null;
  const walk = (node: unknown, path: Path): boolean => {
    if (found) return true;
    if (typeof node === 'string') {
      if (findCitedRange(node, fragment)) { found = path; return true; }
      return false;
    }
    if (Array.isArray(node)) return node.some((v, i) => walk(v, [...path, i]));
    if (isPlainObject(node)) return Object.entries(node).some(([k, v]) => walk(v, [...path, k]));
    return false;
  };
  walk(root, []);
  return found;
}

function getAt(root: unknown, path: Path): unknown {
  return path.reduce<unknown>((acc, key) => {
    if (acc == null) return undefined;
    return (acc as Record<string | number, unknown>)[key];
  }, root);
}

// ── 值单元 ────────────────────────────────────────────────────────────────
function ValueCell({
  value,
  fragment,
  onHitRef,
}: {
  value: unknown;
  fragment?: string;
  onHitRef?: (el: HTMLElement | null) => void;
}) {
  if (value === null || value === undefined || value === '') {
    return <span className="jx-dv-empty">—</span>;
  }
  if (typeof value === 'boolean') {
    return <span className="jx-dv-bool">{value ? t('是') : t('否')}</span>;
  }
  if (typeof value === 'number') {
    return <span className="jx-dv-num">{value.toLocaleString('zh-CN')}</span>;
  }
  const text = String(value);
  if (URL_RE.test(text)) {
    return (
      <a className="jx-dv-link" href={text} target="_blank" rel="noopener noreferrer">{text}</a>
    );
  }
  return <WindowedText className="jx-dv-text" text={text} fragment={fragment} onHitRef={onHitRef} />;
}

interface FieldsProps {
  data: Record<string, unknown>;
  depth: number;
  path: Path;
  focusPath: Path | null;
  fragment?: string;
  onHitRef?: (el: HTMLElement | null) => void;
  /** 已经在卡头露过脸的字段（标题 / 锚点），正文里不再重复 */
  skipKeys?: string[];
}

const onFocusPath = (focusPath: Path | null, path: Path): boolean =>
  !!focusPath && path.every((seg, i) => focusPath[i] === seg);

function Fields({ data, depth, path, focusPath, fragment, onHitRef, skipKeys }: FieldsProps) {
  const skip = new Set(skipKeys || []);
  const entries = Object.entries(data).filter(([k, v]) => !skip.has(k) && v !== undefined);
  if (entries.length === 0) return <div className="jx-dv-empty">{t('暂无内容')}</div>;
  return (
    <div className="jx-dv-fields">
      {entries.map(([key, value]) => (
        <Field
          key={key}
          label={key}
          value={value}
          depth={depth}
          path={[...path, key]}
          focusPath={focusPath}
          fragment={fragment}
          onHitRef={onHitRef}
        />
      ))}
    </div>
  );
}

interface FieldProps {
  label: string;
  value: unknown;
  depth: number;
  path: Path;
  focusPath: Path | null;
  fragment?: string;
  onHitRef?: (el: HTMLElement | null) => void;
}

function Field({ label, value, depth, path, focusPath, fragment, onHitRef }: FieldProps) {
  const inFocus = onFocusPath(focusPath, path);
  const [open, setOpen] = useState(depth < OPEN_DEPTH || inFocus);
  const [shown, setShown] = useState(PAGE);

  // 标量：一行「标签 + 值」；长文本另起一段
  if (!value || typeof value !== 'object') {
    const long = typeof value === 'string' && value.length > LONG_TEXT;
    if (long) {
      return (
        <div className="jx-dv-block">
          <div className="jx-dv-label jx-dv-label--block">{label}</div>
          <div className="jx-dv-longText">
            <ValueCell value={value} fragment={inFocus ? fragment : undefined} onHitRef={inFocus ? onHitRef : undefined} />
          </div>
        </div>
      );
    }
    return (
      <div className="jx-dv-row">
        <div className="jx-dv-label">{label}</div>
        <div className="jx-dv-value"><ValueCell value={value} /></div>
      </div>
    );
  }

  // 数组
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return (
        <div className="jx-dv-row">
          <div className="jx-dv-label">{label}</div>
          <div className="jx-dv-value"><span className="jx-dv-empty">—</span></div>
        </div>
      );
    }
    const allScalar = value.every((v) => !v || typeof v !== 'object');
    if (allScalar) {
      return (
        <div className="jx-dv-row">
          <div className="jx-dv-label">{label}</div>
          <div className="jx-dv-value jx-dv-chips">
            {value.map((v, i) => <span key={i} className="jx-dv-chip">{String(v)}</span>)}
          </div>
        </div>
      );
    }
    const visible = value.slice(0, shown);
    return (
      <div className="jx-dv-group">
        <button type="button" className="jx-dv-groupHead" onClick={() => setOpen(!open)}>
          <span className={`jx-dv-caret${open ? ' jx-dv-caret--open' : ''}`} aria-hidden />
          <span className="jx-dv-groupLabel">{label}</span>
          <span className="jx-dv-groupMeta">{t('共 {n} 条', { n: value.length })}</span>
        </button>
        {open && (
          <div className="jx-dv-cards">
            {visible.map((item, i) => (
              <RecordCard
                key={i}
                index={i}
                value={item}
                depth={depth + 1}
                path={[...path, i]}
                focusPath={focusPath}
                fragment={fragment}
                onHitRef={onHitRef}
              />
            ))}
            {value.length > shown && (
              <button type="button" className="jx-dv-more" onClick={() => setShown(shown + PAGE)}>
                {t('显示更多（还有 {n} 条）', { n: value.length - shown })}
              </button>
            )}
          </div>
        )}
      </div>
    );
  }

  // 子对象
  const obj = value as Record<string, unknown>;
  return (
    <div className="jx-dv-group">
      <button type="button" className="jx-dv-groupHead" onClick={() => setOpen(!open)}>
        <span className={`jx-dv-caret${open ? ' jx-dv-caret--open' : ''}`} aria-hidden />
        <span className="jx-dv-groupLabel">{label}</span>
        <span className="jx-dv-groupMeta">{t('共 {n} 项', { n: Object.keys(obj).length })}</span>
      </button>
      {open && (
        <div className="jx-dv-nested">
          <Fields
            data={obj}
            depth={depth + 1}
            path={path}
            focusPath={focusPath}
            fragment={fragment}
            onHitRef={onHitRef}
          />
        </div>
      )}
    </div>
  );
}

function RecordCard({
  index,
  value,
  depth,
  path,
  focusPath,
  fragment,
  onHitRef,
  bare,
}: {
  index?: number;
  value: unknown;
  depth: number;
  path: Path;
  focusPath: Path | null;
  fragment?: string;
  onHitRef?: (el: HTMLElement | null) => void;
  /** 单条聚焦展示时不套外框，避免卡中卡 */
  bare?: boolean;
}) {
  if (!isPlainObject(value)) {
    return (
      <div className={bare ? 'jx-dv-fields' : 'jx-dv-card'}>
        <ValueCell value={value} fragment={fragment} onHitRef={onHitRef} />
      </div>
    );
  }
  const title = firstTitle(value);
  const citeId = typeof value.cite_id === 'string' ? value.cite_id : '';
  const focused = onFocusPath(focusPath, path);
  const hasHead = !!title || index !== undefined;
  return (
    <div className={`${bare ? 'jx-dv-bareCard' : 'jx-dv-card'}${focused ? ' jx-dv-card--focus' : ''}`}>
      {hasHead && (
        <div className="jx-dv-cardHead">
          {index !== undefined && <span className="jx-dv-cardIdx">{index + 1}</span>}
          {title && <span className="jx-dv-cardTitle">{title.text}</span>}
          {citeId && <span className="jx-tr-citeTag">{citeId}</span>}
        </div>
      )}
      <Fields
        data={value}
        depth={depth}
        path={path}
        focusPath={focusPath}
        fragment={fragment}
        onHitRef={onHitRef}
        skipKeys={hasHead ? [title?.key || '', citeId ? 'cite_id' : ''].filter(Boolean) : []}
      />
    </div>
  );
}

export function DataView({ value, focus, maxHeight = 420, className }: DataViewProps) {
  const parsed = useMemo(() => {
    if (typeof value !== 'string') return value;
    const trimmed = value.trim();
    if (!trimmed || !/^[[{]/.test(trimmed)) return value;
    try { return JSON.parse(trimmed); } catch { return value; }
  }, [value]);

  const rawText = useMemo(() => {
    if (typeof parsed === 'string') return parsed;
    try { return JSON.stringify(parsed, null, 2); } catch { return String(parsed); }
  }, [parsed]);

  const focusPath = useMemo(
    () => (focus ? findFocusPath(parsed, focus) : null),
    [parsed, focus],
  );

  /** 命中片段所在的那条记录（去掉末尾的字段名；数组元素优先） */
  const recordPath = useMemo<Path | null>(() => {
    if (!focusPath || focusPath.length === 0) return null;
    for (let i = focusPath.length - 1; i >= 0; i--) {
      const candidate = focusPath.slice(0, i);
      if (isPlainObject(getAt(parsed, candidate))) return candidate;
    }
    return null;
  }, [focusPath, parsed]);

  // 有命中记录时默认只看这一条；整份结果收在「查看完整结果」后面
  const [focusOnly, setFocusOnly] = useState(true);
  const [raw, setRaw] = useState(false);
  const [uncapped, setUncapped] = useState(false);
  const [overflowing, setOverflowing] = useState(false);
  const [copied, setCopied] = useState(false);

  const bodyRef = useRef<HTMLDivElement | null>(null);
  const hitRef = useRef<HTMLElement | null>(null);

  const narrowed = !!recordPath && recordPath.length > 0 && focusOnly && !raw;
  const shown = narrowed ? getAt(parsed, recordPath!) : parsed;
  const trail = narrowed ? recordPath!.map((seg) => (typeof seg === 'number' ? `#${seg + 1}` : seg)).join(' › ') : '';

  useEffect(() => {
    const wrap = bodyRef.current;
    const hit = hitRef.current;
    if (!wrap || !hit) return;
    const offset = hit.getBoundingClientRect().top - wrap.getBoundingClientRect().top;
    if (offset >= 0 && offset < wrap.clientHeight - 40) return;
    wrap.scrollTop = Math.max(0, wrap.scrollTop + offset - wrap.clientHeight / 3);
  }, [focusPath, raw, narrowed, parsed]);

  useLayoutEffect(() => {
    const wrap = bodyRef.current;
    if (!wrap) return;
    setOverflowing(wrap.scrollHeight > wrap.clientHeight + 4);
  }, [parsed, raw, uncapped, narrowed]);

  const doCopy = () => {
    const done = () => { setCopied(true); setTimeout(() => setCopied(false), 1800); };
    const write = navigator.clipboard?.writeText(rawText);
    if (write) write.then(done).catch(() => { /* 剪贴板不可用时静默 */ }); else done();
  };

  const summary = (() => {
    if (typeof parsed === 'string') return t('{n} 字', { n: parsed.length.toLocaleString('zh-CN') });
    if (Array.isArray(parsed)) return t('共 {n} 条', { n: parsed.length });
    if (isPlainObject(parsed)) return t('共 {n} 项', { n: Object.keys(parsed).length });
    return '';
  })();

  const renderBody = () => {
    if (raw) return <pre className="jx-dv-raw">{rawText}</pre>;
    if (typeof shown === 'string') {
      return (
        <div className="jx-dv-plain">
          <WindowedText text={shown} fragment={focus} onHitRef={(el) => { hitRef.current = el; }} />
        </div>
      );
    }
    if (Array.isArray(shown)) {
      return (
        <Field
          label={t('结果列表')}
          value={shown}
          depth={0}
          path={[]}
          focusPath={focusPath}
          fragment={focus}
          onHitRef={(el) => { hitRef.current = el; }}
        />
      );
    }
    if (isPlainObject(shown)) {
      return (
        <RecordCard
          bare
          value={shown}
          depth={narrowed ? 1 : 0}
          path={narrowed ? recordPath! : []}
          focusPath={focusPath}
          fragment={focus}
          onHitRef={(el) => { hitRef.current = el; }}
        />
      );
    }
    return <div className="jx-dv-plain">{String(shown ?? '')}</div>;
  };

  return (
    <div className={`jx-dv${className ? ` ${className}` : ''}`}>
      <div className="jx-dv-bar">
        <span className="jx-dv-barMeta">
          {narrowed ? t('引用出处：{trail}', { trail }) : summary}
        </span>
        <span className="jx-dv-barActions">
          {!!recordPath && recordPath.length > 0 && !raw && (
            <button type="button" className="jx-dv-barBtn" onClick={() => setFocusOnly(!focusOnly)}>
              {focusOnly ? t('查看完整结果') : t('只看引用出处')}
            </button>
          )}
          {typeof parsed !== 'string' && (
            <button type="button" className="jx-dv-barBtn" onClick={() => setRaw(!raw)}>
              {raw ? t('结构视图') : t('原始 JSON 文本')}
            </button>
          )}
          <button type="button" className="jx-dv-barBtn" onClick={doCopy}>
            {copied ? <CheckOutlined /> : <CopyOutlined />}
            {copied ? t('已复制') : t('复制')}
          </button>
        </span>
      </div>

      <div
        className={`jx-dv-body${overflowing && !uncapped ? ' jx-dv-body--capped' : ''}${raw ? ' jx-dv-body--raw' : ''}`}
        style={uncapped ? undefined : { maxHeight }}
        ref={bodyRef}
      >
        {renderBody()}
      </div>

      {(overflowing || uncapped) && (
        <button type="button" className="jx-dv-expand" onClick={() => setUncapped(!uncapped)}>
          {uncapped ? t('收起内容') : t('展开全部内容')}
        </button>
      )}
    </div>
  );
}

export default DataView;
