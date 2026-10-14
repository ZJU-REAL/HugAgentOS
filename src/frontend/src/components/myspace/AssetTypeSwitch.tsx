import { useId, useRef } from 'react';
import { LayoutGroup, motion, useReducedMotion } from 'motion/react';
import { t } from '../../i18n';
import { SPRING } from '../../utils/motionTokens';
import '../../styles/asset-type-switch.css';

interface AssetTypeSwitchProps {
  value: 'document' | 'image';
  onChange: (value: 'document' | 'image') => void;
}
export function AssetTypeSwitch({ value, onChange }: AssetTypeSwitchProps) {
  const id = useId();
  const reducedMotion = useReducedMotion();
  const refs = useRef<Array<HTMLButtonElement | null>>([]);
  const options = [{ value: 'document', label: t('文档') }, { value: 'image', label: t('图片') }] as const;
  return <LayoutGroup id={id}>
    <div className="jx-assetTypeSwitch" role="radiogroup" aria-label={t('类型筛选')}>
      {options.map((option, index) => <button key={option.value} type="button" role="radio"
        ref={node => { refs.current[index] = node; }} aria-checked={value === option.value}
        tabIndex={value === option.value ? 0 : -1}
        onClick={() => onChange(option.value)}
        onKeyDown={event => {
          const forward = event.key === 'ArrowRight' || event.key === 'ArrowDown';
          const back = event.key === 'ArrowLeft' || event.key === 'ArrowUp';
          if (!forward && !back && event.key !== 'Home' && event.key !== 'End') return;
          event.preventDefault();
          const next = event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1
            : (index + (forward ? 1 : -1) + options.length) % options.length;
          onChange(options[next].value); refs.current[next]?.focus();
        }}>
        {value === option.value && <motion.span className="jx-assetTypeSwitch-thumb" layoutId="asset-type"
          initial={false} transition={reducedMotion ? { duration: 0 } : SPRING.ink} aria-hidden="true" />}
        <span className="jx-assetTypeSwitch-label">{option.label}</span>
      </button>)}
    </div>
  </LayoutGroup>;
}
