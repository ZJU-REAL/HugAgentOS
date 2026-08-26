export const UOS_WINDOW_OPTIONS = Object.freeze({
  autoHideMenuBar: true,
});

export function hideUosWindowMenu(menu, window) {
  menu.setApplicationMenu(null);
  window.setAutoHideMenuBar(true);
  window.setMenuBarVisibility(false);
  window.removeMenu();
}
