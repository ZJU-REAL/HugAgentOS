/**
 * API client for the HugAgentOS backend (v1 unified response envelope).
 *
 * This file is only the stable import entry: the code lives in ./api/ by domain, and
 * `import { … } from '../api'` keeps working for every caller.
 *   core          base URL, hybrid (desktop dual-mode) routing, envelope, 401 handling, apiRequest / authFetch
 *   auth          current user, profile, avatar, SSO session
 *   models        model capabilities, chat modes, capability catalog
 *   chats         sessions, runs, steering, chat shares
 *   interactions  file-write confirmations, design picks, user questions, follow-ups
 *   knowledge     knowledge bases, chunks, LLM wiki
 *   memory        memories, memory / ontology settings
 *   settings      personal API keys, system access, service configs, model providers
 *   capabilities  self-service MCP / skills, marketplaces, desktop capability store
 *   plugins       agent marketplace, plugins, Lark app init
 *   pluginUi      plugin UI contributions
 *   files         uploads, MySpace, personal folders
 *   plans         plan mode, batch jobs, commands
 *   automation    scheduled tasks and notifications
 *   projects      projects, desktop local permissions, tool approval mode
 *   integrations  DingTalk / Lark / email / Yida accounts, channel bots, WeChat binding
 *   loops         autonomous loops
 *   sites         site hosting
 *   logs          my logs and usage
 *   evolution     evolution settlement, contributions, candidates, preferences
 * Smaller feature clients (agentApi, audio, chatForks, chatReruns, projectApi, projectEnvironment)
 * are imported from their own files.
 */
// esbuild (scripts/test-*.ts bundles) does not await an async module (i18n has top-level await) that is
// reached only through `export *` when this file is loaded lazily via `import()`. Plain imports are awaited,
// so every module is imported once for its initialization before being re-exported.
import './api/core';
import './api/auth';
import './api/models';
import './api/chats';
import './api/interactions';
import './api/knowledge';
import './api/memory';
import './api/settings';
import './api/capabilities';
import './api/plugins';
import './api/pluginUi';
import './api/files';
import './api/plans';
import './api/automation';
import './api/projects';
import './api/integrations';
import './api/loops';
import './api/sites';
import './api/logs';
import './api/evolution';

export * from './api/core';
export * from './api/auth';
export * from './api/models';
export * from './api/chats';
export * from './api/interactions';
export * from './api/knowledge';
export * from './api/memory';
export * from './api/settings';
export * from './api/capabilities';
export * from './api/plugins';
export * from './api/pluginUi';
export * from './api/files';
export * from './api/plans';
export * from './api/automation';
export * from './api/projects';
export * from './api/integrations';
export * from './api/loops';
export * from './api/sites';
export * from './api/logs';
export * from './api/evolution';
