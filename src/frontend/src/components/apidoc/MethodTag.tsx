import { Tag } from 'antd';
import { METHOD_COLORS, type HttpMethod } from './apiDocModel';

export function MethodTag({ method }: { method: HttpMethod }) {
  return (
    <Tag style={{
      width: 56,
      textAlign: 'center',
      fontFamily: 'monospace',
      fontWeight: 600,
      margin: 0,
      color: 'var(--color-text-white)',
      background: METHOD_COLORS[method],
      border: 'none',
      lineHeight: '20px',
    }}>
      {method}
    </Tag>
  );
}
