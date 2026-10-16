/** Both layouts project the same available entries; only placement differs. */
export function buildNavigationModel<Item>(options: {
  items: Readonly<Record<string, Item>>;
  primary: readonly string[];
  sidebar: readonly string[];
  menu: readonly string[];
  visible: (key: string, item: Item) => boolean;
}) {
  const entries = new Map(Object.entries(options.items));
  const visible = (key: string) => {
    const item = entries.get(key);
    return item !== undefined && options.visible(key, item);
  };
  const select = (keys: readonly string[]) => [...new Set(keys)].filter(visible);
  const primary = select(options.primary);
  return {
    items: options.items,
    primary,
    sidebar: select(options.sidebar),
    menu: select(options.menu),
    more: select([...options.sidebar, ...options.menu]).filter(key => !primary.includes(key)),
  };
}
