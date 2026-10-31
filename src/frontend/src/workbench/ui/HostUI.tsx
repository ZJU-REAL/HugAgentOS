import type {ComponentProps, ReactNode} from 'react';
import {Button, Checkbox, Input} from 'antd';
import './host-ui.css';

/**
 * The workbench host's only UI layer. Workbench components never render raw
 * <button>/<input>/<select>/<textarea>: `npm run check:controls` rejects them
 * outside this folder, so every host surface inherits the v2 tokens and the
 * antd theme (dark mode included) instead of browser defaults.
 *
 * Native semantics are kept on purpose: labels wrap their control, <select>
 * stays native, and status/alert roles are explicit. Browser tests and screen
 * readers address these by role and label.
 */

type ButtonTone = 'primary' | 'secondary' | 'text' | 'danger';
interface HostButtonProps extends Omit<ComponentProps<typeof Button>, 'type' | 'htmlType' | 'variant' | 'color' | 'danger'> {
  /** One primary per view. Secondary is a filled neutral button; v2 has no outlined buttons. */
  tone?: ButtonTone;
  submit?: boolean;
}
export function HostButton({tone = 'secondary', submit, size, className, ...props}: HostButtonProps) {
  const look = tone === 'primary' ? {type: 'primary' as const}
    : tone === 'text' ? {type: 'text' as const}
    : tone === 'danger' ? {type: 'text' as const, danger: true}
    : {color: 'default' as const, variant: 'filled' as const};
  // autoInsertSpace off: "重试" must not become "重 试" (visible text and accessible name).
  return <Button {...look} {...props} size={size ?? 'middle'} autoInsertSpace={false}
    htmlType={submit ? 'submit' : 'button'} className={'jx-hostButton' + (className ? ' ' + className : '')} />;
}

type NoticeTone = 'info' | 'loading' | 'success' | 'warning' | 'error';
interface HostNoticeProps {
  tone?: NoticeTone;
  /** page: the whole surface is this state (loading, denied, failed); inline: a line inside a form or panel. */
  placement?: 'inline' | 'page';
  children: ReactNode;
  actions?: ReactNode;
}
/** Errors are alerts; everything else is a polite status. One line of text, no spinner. */
export function HostNotice({tone = 'info', placement = 'inline', children, actions}: HostNoticeProps) {
  return <div role={tone === 'error' ? 'alert' : 'status'}
    className={'jx-hostNotice jx-hostNotice--' + tone + ' jx-hostNotice--' + placement}>
    <div className="jx-hostNotice-body">{children}</div>
    {actions && <HostToolbar>{actions}</HostToolbar>}
  </div>;
}

/** Admin/config block: heading, one-line purpose, toolbar, then content. */
export function HostSection({title, description, actions, children, level = 3}: {
  title: string; description?: ReactNode; actions?: ReactNode; children?: ReactNode; level?: 2 | 3;
}) {
  const Heading = level === 2 ? 'h2' : 'h3';
  return <section aria-label={title} className="jx-hostSection">
    <header className="jx-hostSection-header">
      <Heading className="jx-hostSection-title">{title}</Heading>
      {description && <p className="jx-hostSection-description">{description}</p>}
    </header>
    {actions && <HostToolbar>{actions}</HostToolbar>}
    {children}
  </section>;
}

export function HostToolbar({children}: {children: ReactNode}) {
  return <div className="jx-hostToolbar">{children}</div>;
}

/** A standalone hint under a group or form; field hints go through HostField. */
export function HostHint({children}: {children: ReactNode}) {
  return <p className="jx-hostHint">{children}</p>;
}

/** A labelled group of fields. The legend names the group for getByRole('group'). */
export function HostFieldset({legend, disabled, children}: {legend: ReactNode; disabled?: boolean; children: ReactNode}) {
  return <fieldset className="jx-hostFieldset" disabled={disabled}>
    <legend className="jx-hostFieldset-legend">{legend}</legend>
    {children}
  </fieldset>;
}

/** Visible label wrapping its control; the hint sits outside so it never joins the accessible name. */
export function HostField({label, hint, children}: {label: ReactNode; hint?: ReactNode; children: ReactNode}) {
  return <div className="jx-hostField">
    <label className="jx-hostField-control"><span className="jx-hostField-label">{label}</span>{children}</label>
    {hint && <p className="jx-hostField-hint">{hint}</p>}
  </div>;
}

/** Native select: keyboard, form validation and selectOption() behave like the platform. */
export function HostSelect({className, ...props}: ComponentProps<'select'>) {
  return <select {...props} className={'jx-hostSelect' + (className ? ' ' + className : '')} />;
}

// antd controls already carry the theme; re-exported so workbench imports stay in one place.
export {Checkbox as HostCheckbox, Input as HostInput};
export const HostTextArea = Input.TextArea;

/** Secondary text: revision, keys, counts. */
export function HostMeta({children}: {children: ReactNode}) {
  return <span className="jx-hostMeta">{children}</span>;
}
