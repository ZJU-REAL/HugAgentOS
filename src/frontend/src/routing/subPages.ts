import type { AbilityTabKey, KbTabKey, MySpaceTab } from '../types';
import { pathForPanel, subsFromPath } from './navigation';
import { useRouteSubs } from './usePanel';

/**
 * 各面板内部的下级页与地址段的对应关系，集中在这里一处。
 *
 * 之所以要有这张表：地址段是给人看、给人分享的，不必等于代码里的内部键
 * （`mcp` 这个历史键对外叫「连接器」）。两边分开写迟早会分叉，所以只此一张。
 *
 * 取值一律从地址算，不在任何 store 或浏览器存储里另存一份。
 */
const ABILITY_SLUGS: Record<AbilityTabKey, string> = {
  agents: 'agents',
  skills: 'skills',
  mcp: 'connectors',
  plugins: 'plugins',
};

const ABILITY_BY_SLUG = new Map(
  (Object.entries(ABILITY_SLUGS) as Array<[AbilityTabKey, string]>).map(([k, v]) => [v, k]),
);

const MY_SPACE_TABS: readonly MySpaceTab[] = ['assets', 'kb', 'favorites', 'shares', 'notifications'];
const KB_TABS: readonly KbTabKey[] = ['public', 'private'];

export function abilitySlug(tab: AbilityTabKey): string {
  return ABILITY_SLUGS[tab];
}

export function abilityTabFromSubs(subs: string[]): AbilityTabKey {
  return ABILITY_BY_SLUG.get(subs[0] ?? '') ?? 'agents';
}

export function mySpaceTabFromSubs(subs: string[]): MySpaceTab {
  const first = subs[0] as MySpaceTab | undefined;
  return first && MY_SPACE_TABS.includes(first) ? first : 'assets';
}

/** Knowledge-base categories belong to My Space. */
export function kbTabFrom(pathname?: string): KbTabKey {
  const raw = subsFromPath(pathname)[1] as KbTabKey | undefined;
  return raw && KB_TABS.includes(raw) ? raw : 'public';
}
export function pathForKbTab(tab: KbTabKey): string {
  return pathForPanel('my_space', 'kb', tab);
}

export function useAbilityTab(): AbilityTabKey {
  return abilityTabFromSubs(useRouteSubs());
}

export function useMySpaceTab(): MySpaceTab {
  return mySpaceTabFromSubs(useRouteSubs());
}

export function useKbTab(): KbTabKey {
  // useRouteSubs 已订阅地址变化，这里再读一次当前地址即可保持同步
  useRouteSubs();
  return kbTabFrom();
}

/** 设置页的子菜单；默认落在「个人信息」。 */
export function useSettingsSection(defaultId: string): string {
  return useRouteSubs()[0] || defaultId;
}
