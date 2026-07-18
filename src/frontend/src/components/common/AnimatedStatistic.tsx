import { useEffect } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import { Statistic } from 'antd';
import { animate, motion, useMotionValue, useTransform } from 'motion/react';

interface AnimatedStatisticProps {
  title?: ReactNode;
  /** Target value; on change, rolls from the currently displayed value to the new value */
  value: number;
  /** Number of decimal places (pass 4 for cost-type values); 0 means round to integer and add thousands separators */
  precision?: number;
  prefix?: ReactNode;
  suffix?: ReactNode;
  valueStyle?: CSSProperties;
}

/**
 * A number-rolling Statistic: on value change, rolls to the new value over 0.6s easeOut.
 * MotionValue drives the text node directly; when polling returns the same value the effect doesn't fire and won't replay.
 */
export function AnimatedStatistic({
  title, value, precision = 0, prefix, suffix, valueStyle,
}: AnimatedStatisticProps) {
  const mv = useMotionValue(0);
  const display = useTransform(mv, (v) => (
    precision > 0 ? v.toFixed(precision) : Math.round(v).toLocaleString()
  ));

  useEffect(() => {
    const controls = animate(mv, value, { duration: 0.6, ease: 'easeOut' });
    return () => controls.stop();
  }, [mv, value]);

  return (
    <Statistic
      title={title}
      value={value}
      prefix={prefix}
      suffix={suffix}
      valueStyle={valueStyle}
      formatter={() => <motion.span>{display}</motion.span>}
    />
  );
}
