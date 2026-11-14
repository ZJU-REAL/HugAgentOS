import assert from 'node:assert/strict';
import { moduleNavigation, secondaryModule } from '../src/components/sidebar/moduleNavigation';
import { mergePageConfig, normalizeLayoutNavigation } from '../src/utils/pageConfigDefaults';
const ids = (items: ReturnType<typeof moduleNavigation>['more']) => items.map(item => item.id);
const open = {capabilities: {lab_enabled: true, sites_enabled: true}, localOnly: false, automationAllowed: true};

// Unset layouts keep the product default.
const fallback = moduleNavigation(undefined, open);
assert.deepEqual(ids(fallback.primary), ['my_space','automation','ability_center']);
assert.deepEqual(ids(fallback.more), ['projects','sites','app_center','lab']);
for (const panel of ['projects','sites','app_center','lab'] as const) assert.equal(secondaryModule(panel, fallback.more), true);
assert.equal(secondaryModule('workspace', fallback.more), false);

// The administrator's order and placement are followed; apps, settings, duplicates and cross-bucket repeats are dropped.
const config = {sidebar_items: ['sites','sites','ability_center','external:report','my_space'], more_items: ['lab','projects','settings','plugin:report','sites','app_center','projects']};
const full = moduleNavigation(config as never, open);
assert.deepEqual(ids(full.primary), ['sites','ability_center','my_space']);
assert.deepEqual(ids(full.more), ['lab','projects','app_center'], 'automation is hidden because it is in neither bucket');

const restricted = moduleNavigation(config as never, {capabilities: {lab_enabled: false, sites_enabled: false}, localOnly: true, automationAllowed: false});
assert.deepEqual(ids(restricted.primary), ['ability_center']);
assert.deepEqual(ids(restricted.more), ['projects','app_center']);

// Lab and sites are separate permissions.
const labOff = moduleNavigation(undefined, {...open, capabilities: {lab_enabled: false, sites_enabled: true}});
assert.deepEqual(ids(labOff.more), ['projects','sites','app_center'], 'turning lab off keeps sites');
const sitesOff = moduleNavigation(undefined, {...open, capabilities: {lab_enabled: true, sites_enabled: false}});
assert.deepEqual(ids(sitesOff.more), ['projects','app_center','lab'], 'turning sites off keeps lab');

const emptied = moduleNavigation({sidebar_items: [], more_items: ['projects']}, open);
assert.deepEqual(ids(emptied.primary), [], 'an empty primary list is respected');
assert.deepEqual(ids(emptied.more), ['projects']);

// Each layout keeps its own arrangement after merging the remote config.
const merged = mergePageConfig({navigation: {layouts: {
  classic: {sidebar_items: ['projects','my_space'], more_items: ['lab']},
}}} as never);
assert.deepEqual(merged.navigation.layouts.classic, {sidebar_items: ['projects','my_space'], more_items: ['lab']});
assert.deepEqual(merged.navigation.layouts.modern, normalizeLayoutNavigation(undefined));
assert.deepEqual(normalizeLayoutNavigation({sidebar_items: ['lab']}).more_items, ['projects','sites','app_center'],
  'a missing more list falls back to the default without repeating primary entries');
console.log('module navigation: per-layout placement, order, deduplication, app exclusion, secondary navigation, lab/sites permissions and visibility passed');
