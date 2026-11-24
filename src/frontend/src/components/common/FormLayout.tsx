// Layout primitives for dialogs and forms that would otherwise stack every field
// one per row and push the footer below the fold:
// - FormGrid: short fields (numbers, selects, switches) side by side, collapsing to
//   one column when the dialog is narrow (container query, not viewport).
// - ToggleField: a switch as one compact row (label left, switch right) instead of a
//   label row plus a switch row.
// - AdvancedFields: rarely-touched settings behind one text toggle. Children stay
//   mounted while collapsed, so antd Form still reads, validates and submits them.
// Modal sizing (COMPACT_MODAL_PROPS) lives in ./modalLayout.
import { useState, type CSSProperties, type ReactElement, type ReactNode } from 'react';
import { Button, Form, Switch, Tooltip } from 'antd';
import { QuestionCircleOutlined, RightOutlined } from '@ant-design/icons';
import type { NamePath } from 'antd/es/form/interface';
import { t } from '../../i18n';

interface FormGridProps {
  /** Columns at full width (2 or 3). Drops to fewer columns in narrow containers. */
  cols?: 2 | 3;
  className?: string;
  children: ReactNode;
}

export function FormGrid({ cols = 2, className, children }: FormGridProps) {
  return (
    <div className="jx-formGridWrap">
      <div
        className={`jx-formGrid${className ? ` ${className}` : ''}`}
        style={{ '--jx-form-cols': cols } as CSSProperties}
      >
        {children}
      </div>
    </div>
  );
}

/** A FormGrid cell that spans every column. */
export function FormFull({ children }: { children: ReactNode }) {
  return <div className="jx-formGrid-full">{children}</div>;
}

interface ToggleFieldProps {
  name: NamePath;
  label: string;
  tooltip?: ReactNode;
  /** One short line under the label; longer explanations belong in `tooltip`. */
  description?: ReactNode;
  disabled?: boolean;
  /** Replace the default Switch (must accept checked / onChange). */
  control?: ReactElement;
  /** Value prop of the control (default `checked`). */
  valuePropName?: string;
  className?: string;
}

export function ToggleField({
  name, label, tooltip, description, disabled, control, valuePropName = 'checked', className,
}: ToggleFieldProps) {
  return (
    <div className={`jx-formToggle${className ? ` ${className}` : ''}`}>
      <div className="jx-formToggle-text">
        <span className="jx-formToggle-label">
          {label}
          {tooltip && (
            <Tooltip title={tooltip}>
              <QuestionCircleOutlined className="jx-formToggle-help" aria-label={t('说明')} />
            </Tooltip>
          )}
        </span>
        {description && <span className="jx-formToggle-desc">{description}</span>}
      </div>
      <Form.Item name={name} valuePropName={valuePropName} noStyle>
        {control ?? <Switch size="small" disabled={disabled} aria-label={label} />}
      </Form.Item>
    </div>
  );
}

interface AdvancedFieldsProps {
  title?: string;
  /** Short summary shown next to the title while collapsed, e.g. which settings live inside. */
  hint?: string;
  defaultOpen?: boolean;
  children: ReactNode;
}

export function AdvancedFields({ title, hint, defaultOpen = false, children }: AdvancedFieldsProps) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className={`jx-formAdvanced${open ? ' jx-formAdvanced--open' : ''}`}>
      <Button
        type="text"
        size="small"
        className="jx-formAdvanced-toggle"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        icon={<RightOutlined className="jx-formAdvanced-chevron" />}
      >
        {title ?? t('高级设置')}
        {hint && !open && <span className="jx-formAdvanced-hint">{hint}</span>}
      </Button>
      <div className={`jx-expandWrap${open ? ' jx-expandWrap--open' : ''}`}>
        <div className="jx-formAdvanced-body" inert={!open}>
          {children}
        </div>
      </div>
    </div>
  );
}
