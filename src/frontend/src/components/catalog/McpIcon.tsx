import { useState } from 'react';
import { normalizeMcpIconUrl } from '../../utils/iconLibrary';

/** Shared connector icon used by the ability center and invocation menus. */
export function McpIcon({ id, icon, size }: { id: string; icon?: string; size?: number }) {
  const normalizedIcon = normalizeMcpIconUrl(icon);
  const [failedIcon, setFailedIcon] = useState('');
  const wrapStyle = size ? { width: size, height: size, flex: `0 0 ${size}px` } : undefined;
  const imageStyle = size
    ? { width: Math.max(12, size - 4), height: Math.max(12, size - 4) }
    : undefined;

  if (normalizedIcon && failedIcon !== normalizedIcon) {
    return (
      <div className="jx-mcp-iconWrap" style={wrapStyle}>
        <img
          src={normalizedIcon}
          alt=""
          className="jx-mcp-iconImg"
          style={imageStyle}
          onError={() => setFailedIcon(normalizedIcon)}
        />
      </div>
    );
  }
  return (
    <div className="jx-mcp-iconWrap jx-mcp-iconFallback" style={wrapStyle}>
      <span>{(id || '?')[0].toUpperCase()}</span>
    </div>
  );
}
