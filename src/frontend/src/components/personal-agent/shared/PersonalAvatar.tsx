import { presetOf } from './avatarPresets';

interface PersonalAvatarProps {
  avatar?: string;
  name: string;
  size?: number;
}

/** The agent's avatar: an uploaded image or a built-in glyph on a tinted tile. */
export function PersonalAvatar({ avatar, name, size = 40 }: PersonalAvatarProps) {
  const preset = presetOf(avatar);
  const style = { width: size, height: size, fontSize: Math.round(size * 0.56) };
  if (!preset && avatar) return <img className="pa-avatar" src={avatar} alt={name} style={style} />;
  return <span className={'pa-avatar pa-avatar--' + (preset?.tint || 'blue')} role="img" aria-label={name} style={style}>{preset?.glyph}</span>;
}
