import { extractArtifactOutputs } from '../../utils/fileParser';
import type { ChatMessage } from '../../types';
import { ArtifactCardList, type ArtifactRef } from './ArtifactCardList';
  /** Render file download/image artifact cards */
export function MessageArtifacts({ m, currentChatId }: { m: ChatMessage; currentChatId: string }) {
    if (!m.toolCalls || m.isStreaming) return null;
    const artifactMap = new Map<string, ArtifactRef>();
    const pushArtifact = (artifact: Record<string, unknown>) => {
      if (!artifact?.file_id) return;
      artifactMap.set(String(artifact.file_id), { ...artifact, file_id: String(artifact.file_id) } as ArtifactRef);
    };
    for (const tool of m.toolCalls) {
      const out = tool.output;
      if (tool.status !== 'success' && tool.status != null) {
        continue;
      }
      // pin_to_workspace returns a {file_id, name, ...} shape that would
      // otherwise render as a duplicate card. The workspace_files allowlist
      // (handled below) already covers what should be shown, so suppress
      // the pin tool's own output from contributing artifact entries.
      if (tool.name !== 'pin_to_workspace') {
        for (const artifact of extractArtifactOutputs(out)) {
          pushArtifact(artifact);
        }
      }
    }
    // Strict workspace gate: pin_to_workspace is the only way for a file
    // to surface in the conversation. workspaceFiles is an array (possibly
    // empty) on every new message — empty means the agent didn't pin
    // anything, so nothing renders. When the field is missing entirely
    // (undefined) the message predates this feature → legacy fallback
    // (show every artifact extracted from tool outputs).
    let artifacts = Array.from(artifactMap.values());
    if (Array.isArray(m.workspaceFiles)) {
      const allow = new Set(m.workspaceFiles);
      artifacts = artifacts.filter((a) => allow.has(String(a.file_id)));
    }
    if (artifacts.length === 0) return null;
    return <ArtifactCardList artifacts={artifacts} chatId={currentChatId} />;
}
