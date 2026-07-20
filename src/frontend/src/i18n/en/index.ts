/** 各域英文字典合并出口。key 为中文原文；重复 key 后者覆盖（应保持译文一致）。 */
import { CORE_DICT } from './core';
import { CHAT_DICT } from './chat';
import { HOOKS_DICT } from './hooks';
import { PANELS_DICT } from './panels';
import { TOOL_DICT } from './tool';
import { CATALOG_DICT } from './catalog';
import { SETTINGS_DICT } from './settings';
import { MYSPACE_DICT } from './myspace';
import { LAB_DICT } from './lab';
import { ADMIN_SKILLS_DICT } from './adminSkills';
import { ADMIN_CONTENT_DICT } from './adminContent';
import { ADMIN_SYSTEM_DICT } from './adminSystem';
import { ADMIN_MISC_DICT } from './adminMisc';
import { CONFIG_A_DICT } from './configA';
import { CONFIG_B_DICT } from './configB';
import { CONFIG_ROLES_DICT } from './configRoles';
import { STORES_DICT } from './stores';
import { APIDOC_DICT } from './apidoc';
import { DB_METADATA_DICT } from './dbMetadata';
import { AGENT_MARKET_DICT } from './agentMarket';
import { ONBOARDING_DICT } from './onboarding';

export const EN_DICT: Record<string, string> = {
  ...CORE_DICT,
  ...CHAT_DICT,
  ...HOOKS_DICT,
  ...PANELS_DICT,
  ...TOOL_DICT,
  ...CATALOG_DICT,
  ...SETTINGS_DICT,
  ...MYSPACE_DICT,
  ...LAB_DICT,
  ...ADMIN_SKILLS_DICT,
  ...ADMIN_CONTENT_DICT,
  ...ADMIN_SYSTEM_DICT,
  ...ADMIN_MISC_DICT,
  ...CONFIG_A_DICT,
  ...CONFIG_B_DICT,
  ...CONFIG_ROLES_DICT,
  ...STORES_DICT,
  ...APIDOC_DICT,
  ...DB_METADATA_DICT,
  ...AGENT_MARKET_DICT,
  ...ONBOARDING_DICT,
};
