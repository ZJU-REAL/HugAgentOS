import assert from 'node:assert/strict';
import { canInitializeProject, isProjectInitCommand } from '../src/utils/projectCommands';
import { useProjectStore } from '../src/stores/projectStore';
import type { ProjectDetail } from '../src/types';

const allowed = { projectId: 'p1', permission: 'edit', busy: false, hasCapability: false, specialMode: false };
assert.equal(canInitializeProject(allowed), true);
for (const override of [
  { projectId: null }, { permission: 'view' }, { permission: undefined },
  { busy: true }, { hasCapability: true }, { specialMode: true },
]) {
  assert.equal(canInitializeProject({ ...allowed, ...override }), false);
}
for (const text of ['/init', '/初始化指令', ' /init ']) assert.equal(isProjectInitCommand(text), true);
for (const text of ['普通消息', '/init more', 'Explain /init']) assert.equal(isProjectInitCommand(text), false);

type Controls = { read: () => Promise<ProjectDetail>; write: (...args: unknown[]) => Promise<ProjectDetail>; files: () => Promise<unknown> };
const controls = (globalThis as unknown as { projectTestApi: Controls }).projectTestApi;
const project = (id: string, text: string, revision: string) => ({
  project_id: id, instructions: text, instructions_revision: revision, permission: 'admin',
} as ProjectDetail);
const before = project('p1', 'Before', 'v1');
const after = project('p1', 'Saved', 'v2');
useProjectStore.setState({ currentProjectId: 'p1', currentProject: before });
let resolvePoll!: (value: ProjectDetail) => void;
controls.read = () => new Promise((resolve) => { resolvePoll = resolve; });
const poll = useProjectStore.getState().refreshInstructions();
controls.write = async (...args) => {
  assert.deepEqual(args, ['p1', 'Saved', 'v1']);
  return after;
};
controls.files = async () => { throw new Error('file inventory temporarily unavailable'); };
await useProjectStore.getState().updateInstructions('Saved', 'v1');
resolvePoll(before);
await poll;
assert.equal(useProjectStore.getState().currentProject?.instructions, 'Saved', 'slow polling must not undo a saved result');

const switchPoll = useProjectStore.getState().refreshInstructions();
const second = project('p2', 'Other project', 'v9');
useProjectStore.setState({ currentProjectId: 'p2', currentProject: second });
resolvePoll(after);
await switchPoll;
assert.equal(useProjectStore.getState().currentProject, second, 'late responses must not replace another project');

// Failed PATCH keeps the current instructions and propagates a visible conflict.
controls.write = async () => { throw new Error('409 conflict'); };
await assert.rejects(useProjectStore.getState().updateInstructions('Stale', 'v1'), /409 conflict/);
assert.equal(useProjectStore.getState().currentProject, second);
console.log('Project command eligibility and instruction-store acceptance passed.');
