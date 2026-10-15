import type { ComponentType } from 'react';

import type {RegisteredApplication} from '@hugagent/workbench-protocol';
export type WorkbenchApplication = RegisteredApplication;

/** The caller supplies trusted implementations. Declarations never resolve import paths. */
export function createWorkbenchRegistry<Props extends object>(options: {
  applications: readonly WorkbenchApplication[];
  components: Readonly<Record<string, ComponentType<Props>>>;
  commands: Readonly<Record<string, () => void>>;
}) {
  const components = new Map(Object.entries(options.components));
  const commands = new Map(Object.entries(options.commands));
  const applications = new Map<string, WorkbenchApplication>();
  for (const app of options.applications) {
    if (!app.id || applications.has(app.id)) throw new Error(`Duplicate or empty application ID: ${app.id}`);
    const target = app.target;
    if (target.kind === 'builtin' && !components.has(target.componentId)) {
      throw new Error(`Unknown component: ${target.componentId}`);
    }
    if (target.kind === 'command' && !commands.has(target.commandId)) {
      throw new Error(`Unknown command: ${target.commandId}`);
    }
    applications.set(app.id, Object.freeze({ id: app.id, target: Object.freeze({ ...target }) }));
  }
  const get = (id: string) => {
    const app = applications.get(id);
    if (!app) throw new Error(`Unknown application: ${id}`);
    return app;
  };
  return Object.freeze({
    applications: Object.freeze([...applications.values()]),
    get,
    component(id: string): ComponentType<Props> {
      const target = get(id).target;
      if (target.kind !== 'builtin') throw new Error(`Application is not a page: ${id}`);
      return components.get(target.componentId)!;
    },
    executeCommand(id: string) {
      const target = get(id).target;
      if (target.kind !== 'command') throw new Error(`Application is not a command: ${id}`);
      commands.get(target.commandId)!();
    },
  });
}
