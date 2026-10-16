import { useEffect, useRef, useState } from 'react';
import type { ApplicationEntry } from './applicationEntries';
import type { PanelKey } from './types';
import { LAYOUT_ITEMS } from './components/sidebar/items';
import { builtinCommands } from './applicationCommands';
import { apiRequest } from './api';
import { navigateTo, pathForPanel } from './routing/navigation';
import { openExternalApplication } from './workbench/openExternal';
import { externalErrorMessage } from './workbench/externalApi';
import { useApplicationDirectory } from './workbench/catalog';
import {openWorkspaceApplication} from './workbench/workspaceNavigation';

/** Business routes and commands are injected here, outside the workbench renderer. */
export function useApplicationOpen(select?: (panel: PanelKey, sub?: string) => void,
  openPlugin?: (key: string, sub?: string) => void) {
  const {userId} = useApplicationDirectory();
  const [feedback, setFeedback] = useState('');
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => { request.current?.abort(); }, [userId]);
  function open(entry: ApplicationEntry, sub?: string) {
    if (!entry.item.availability.available) return;
    request.current?.abort();
    const target = entry.item.declaration.target;
    const controller = new AbortController(); request.current = controller;
    setFeedback('');
    const selectBuiltin = select || ((panel: PanelKey, path?: string) => navigateTo(pathForPanel(panel, path)));
    if (target.kind === 'builtin') {
      const meta = LAYOUT_ITEMS[entry.item.declaration.id];
      if (meta) selectBuiltin(meta.targetPanel, sub);
      else if (['chat', 'settings', 'docs'].includes(entry.item.declaration.id)) selectBuiltin(entry.item.declaration.id as PanelKey, sub);
      return;
    }
    if (target.kind === 'plugin' || (target.kind === 'external' && target.mode === 'embedded')) {
      if (openPlugin) openPlugin(entry.key, sub);
      else openWorkspaceApplication(entry.key, sub || '/');
      return;
    }
    void (async () => {
      try {
        if (target.kind === 'external') await openExternalApplication(entry.key, controller.signal);
        else {
          const response = await apiRequest<{data:{applicationKey:string;target:{kind:string;commandId?:string}}}>(
            '/v1/workbench/applications/' + encodeURIComponent(entry.key) + '/launch',
            {method:'POST',body:'{}',signal:controller.signal});
          if (!controller.signal.aborted && response.data.applicationKey === entry.key &&
            response.data.target.kind === 'command' && response.data.target.commandId === target.commandId)
            builtinCommands.executeCommand(entry.item.declaration.id);
        }
      } catch (error) { if (!controller.signal.aborted) setFeedback(externalErrorMessage(error)); }
    })();
  }
  return {open,feedback};
}
