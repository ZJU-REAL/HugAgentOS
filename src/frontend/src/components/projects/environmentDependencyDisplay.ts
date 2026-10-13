import type {
  EnvironmentField, EnvironmentOptions, EnvironmentPreview, ProjectEnvironment,
} from '../../api/projectEnvironment';

/** Plugin components stay inside their plugin; explicit bindings remain visible. */
export function visibleEnvironmentDependencies(
  config: ProjectEnvironment,
  options: EnvironmentOptions | null,
  preview?: EnvironmentPreview,
): EnvironmentPreview['dependencies'] {
  if (!preview || !options) return [];
  const inventory = Object.fromEntries(
    Object.entries(options).map(([field, items]) => [field, new Map(items.map(item => [item.id, item]))]),
  ) as Record<EnvironmentField, Map<string, EnvironmentOptions[EnvironmentField][number]>>;
  const pluginIds = new Set([
    ...config.plugin_ids,
    ...(preview.effective.plugin_ids || []),
    ...preview.dependencies.filter(item => item.kind === 'plugin_ids').map(item => item.id),
  ]);
  const pending: Array<[EnvironmentField, string]> =
    [...pluginIds].map(id => ['plugin_ids', id]);
  const contained = new Set<string>();
  while (pending.length) {
    const [field, id] = pending.pop()!;
    const key = `${field}:${id}`;
    if (contained.has(key)) continue;
    contained.add(key);
    const item = inventory[field].get(id);
    for (const [dependencyField, ids] of Object.entries(item?.dependencies || {})) {
      for (const dependencyId of ids) pending.push([dependencyField as EnvironmentField, dependencyId]);
    }
  }
  return preview.dependencies.filter(item =>
    (item.kind !== 'skill_ids' && item.kind !== 'mcp_ids') ||
    !contained.has(`${item.kind}:${item.id}`),
  );
}
