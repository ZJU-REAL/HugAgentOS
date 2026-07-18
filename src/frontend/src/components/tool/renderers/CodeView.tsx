import { useMemo, type ReactNode } from 'react';
import { LANG_LABELS, highlightCode } from '../../../utils/codeExecUtils';
import { t } from '../../../i18n';

interface CodeViewProps {
  code: string;
  /** highlight.js language id (python / bash / typescript / …). */
  language: string;
  /** Optional buttons rendered on the right of the code bar (copy, expand…). */
  actions?: ReactNode;
  /** Extra class on the outer section (e.g. for max-height tweaks). */
  className?: string;
}

/**
 * Syntax-highlighted code block: language bar + line numbers + highlighted
 * <pre>. Shared by the code-execution result view and the running code-tool
 * card so the markup/highlight logic lives in one place.
 */
export function CodeView({ code, language, actions, className }: CodeViewProps) {
  const highlighted = useMemo(() => highlightCode(code, language), [code, language]);
  const lineCount = useMemo(() => code.split('\n').length, [code]);

  if (!code) return null;

  return (
    <div className={`jx-ce-codeSection${className ? ` ${className}` : ''}`}>
      <div className="jx-ce-codeBar">
        <div className="jx-ce-codeBarLeft">
          <span className="jx-ce-langDot" data-lang={language} />
          <span className="jx-ce-langLabel">{LANG_LABELS[language] || language}</span>
          <span className="jx-ce-lineCount">{t('{n} 行', { n: lineCount })}</span>
        </div>
        {actions && (
          <div className="jx-ce-codeBarActions" style={{ opacity: 1 }}>{actions}</div>
        )}
      </div>
      <div className="jx-ce-codeWrap">
        <div className="jx-ce-lineNums" aria-hidden="true">
          {Array.from({ length: lineCount }, (_, i) => <span key={i}>{i + 1}</span>)}
        </div>
        <pre className="jx-ce-code">
          <code
            className={`hljs language-${language}`}
            dangerouslySetInnerHTML={{ __html: highlighted }}
          />
        </pre>
      </div>
    </div>
  );
}
