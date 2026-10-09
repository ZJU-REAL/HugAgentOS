import { reduceControls } from './controls';
import { reduceLifecycleAndTools } from './lifecycleAndTools';
import { useUIStore } from '../../stores';
import type { ChatStreamState } from './state';
import { reduceToolResultsAndMeta } from './toolResultsAndMeta';

/** Install the compacted current controls through the shared domain reducers. */
export function installRunControls(
  state: ChatStreamState,
  controls: Record<string, Record<string, unknown>>,
) {
  const ui = useUIStore.getState();
  ui.clearPendingConfirm(state.chatId);
  ui.setPendingDesignPick(state.chatId, null);
  ui.hydratePendingUserQuestionQueue(state.chatId, []);
  const phase = state.thinkingPhaseActive;
  const buffer = state.parseBuffer;
  state.parseBuffer = '';
  state.ontologyGovernance = undefined;
  state.evolutionSummary = undefined;
  state.metaFollowUps = [];
  state.metaDurationMs = null;
  state.metaWorkspaceFiles = null;
  state.compactionPending = false;
  state.queuedRun = undefined;
  try {
    for (const control of Object.values(controls)) {
      const type = String(control.type ?? '');
      // An error is a task verdict only in the terminal path; its text is
      // installed after the message state, through the ordinary error reducer.
      if (reduceLifecycleAndTools(state, control, control, type, control.scope === 'ontology_revision')) continue;
      if (reduceToolResultsAndMeta(state, control, control, type)) continue;
      reduceControls(state, control, control, type);
    }
  } finally {
    state.thinkingPhaseActive = phase;
    state.parseBuffer = buffer;
  }
}
