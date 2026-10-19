import './application-icon.css';
import { useEffect, useState } from 'react';
import { useUIStore } from '../stores/uiStore';
import type { ApplicationDeclaration } from '@hugagent/workbench-protocol';

function safeImage(value: string | undefined) {
  if (!value) return undefined;
  try {
    const url = new URL(value, window.location.origin);
    return ['http:', 'https:'].includes(url.protocol) ? url.href : undefined;
  } catch { return undefined; }
}

/** Untrusted SVG stays an image. Color assets are never recolored by host CSS. */
export function ApplicationIcon({ icon, builtinIcon, className = '' }: {
  icon?: ApplicationDeclaration['icon']; builtinIcon?: string; className?: string;
}) {
  const theme = useUIStore(s => s.themeMode);
  const [systemDark, setSystemDark] = useState(() => window.matchMedia('(prefers-color-scheme: dark)').matches);
  useEffect(() => { const media = window.matchMedia('(prefers-color-scheme: dark)'); const update = () => setSystemDark(media.matches); media.addEventListener('change', update); return () => media.removeEventListener('change', update); }, []);
  const dark = theme === 'dark' || (theme === 'system' && systemDark);
  const source = safeImage((dark && icon?.dark) || icon?.light || builtinIcon);
  const [failed, setFailed] = useState('');
  return source && failed !== source
    ? <img className={'jx-applicationIcon ' + className} data-color-icon={icon ? 'true' : undefined}
      src={source} alt="" aria-hidden="true" onError={() => setFailed(source)} />
    : <span className={'jx-applicationIcon jx-applicationIconFallback ' + className} aria-hidden="true">▦</span>;
}
