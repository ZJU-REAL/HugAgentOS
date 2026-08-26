function desktopActionUrl(route: 'menu' | 'win', action: string): string {
  return `/__desktop/${route}?action=${encodeURIComponent(action)}`;
}

export function desktopMenuActionUrl(action: string): string {
  return desktopActionUrl('menu', action);
}

export function desktopWindowActionUrl(action: string): string {
  return desktopActionUrl('win', action);
}
