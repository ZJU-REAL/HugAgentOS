/**
 * Plugin UI material library — public surface.
 *
 * Deliberately small: only what host code actually consumes. Everything else
 * (registry, individual views, pointer internals) is library-internal — host
 * code imports from here, contract types from `./types`, so the library's
 * internal layout stays free to change.
 */

export { PluginView, type PluginViewProps } from './PluginView';
export { PluginModuleFrame, type PluginModuleFrameProps } from './module/PluginModuleFrame';
export { resolveText, fillTemplate } from './i18n';
export { readText } from './pointer';
