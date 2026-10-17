/** Built-in avatars. The value saved on the agent is `preset:<id>`; uploads are data URLs. */
export const AVATAR_PRESETS = [
  { id: 'robot', glyph: '🤖', tint: 'blue' },
  { id: 'fox', glyph: '🦊', tint: 'amber' },
  { id: 'cat', glyph: '🐱', tint: 'purple' },
  { id: 'owl', glyph: '🦉', tint: 'teal' },
  { id: 'panda', glyph: '🐼', tint: 'green' },
  { id: 'star', glyph: '⭐', tint: 'amber' },
  { id: 'bolt', glyph: '⚡', tint: 'cyan' },
  { id: 'leaf', glyph: '🍃', tint: 'green' },
] as const;

export type AvatarPreset = (typeof AVATAR_PRESETS)[number];

/** Default name, after HugAgentOS. Users rename it in settings or by asking the agent. */
export const DEFAULT_AGENT_NAME = 'Hugo';

export function presetOf(avatar?: string): AvatarPreset | undefined {
  if (!avatar?.startsWith('preset:')) return avatar ? undefined : AVATAR_PRESETS[0];
  return AVATAR_PRESETS.find(item => item.id === avatar.slice(7)) || AVATAR_PRESETS[0];
}

/** Square-crop and downscale an uploaded image to a small JPEG data URL. */
export function resizeAvatar(file: File, size = 256): Promise<string> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const image = new Image();
    image.onload = () => {
      const side = Math.min(image.width, image.height);
      const canvas = document.createElement('canvas');
      canvas.width = size; canvas.height = size;
      const context = canvas.getContext('2d');
      if (!context) { URL.revokeObjectURL(url); reject(new Error('Canvas unavailable')); return; }
      context.drawImage(image, (image.width - side) / 2, (image.height - side) / 2, side, side, 0, 0, size, size);
      URL.revokeObjectURL(url);
      resolve(canvas.toDataURL('image/jpeg', 0.86));
    };
    image.onerror = () => { URL.revokeObjectURL(url); reject(new Error('Image unreadable')); };
    image.src = url;
  });
}
