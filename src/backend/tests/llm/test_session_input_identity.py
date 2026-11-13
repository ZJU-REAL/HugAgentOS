"""Live completion inputs must coexist with replayed history without ID collisions."""

from copy import deepcopy
import unittest

from core.llm.context.adapter import AgentScopeContextAdapter
from core.llm.context.ir import ContextAssembler
from core.llm.context.ir_constants import SESSION_CONTEXT_META_KEY
from core.llm.context.rendering import next_request_sequence, render_session_input
from core.sandbox.commands.context import completion_context


class SessionInputIdentityTests(unittest.TestCase):
    def setUp(self):
        self.adapter = AgentScopeContextAdapter()
        self.history = [self.adapter.message_from_session_dict(
            {"role": "user", "content": "Generate the requested document"}, created_seq=0
        )]

    def assemble(self, messages):
        items = self.adapter.items_from_messages(messages)
        assembly = ContextAssembler(total_budget=10000).assemble(items)
        self.assertEqual(len(assembly.included), len(items))
        return items

    def test_command_completion_resumes_beside_original_user_input(self):
        row = completion_context({
            "task_id": "cmd_original", "status": "completed", "exit_code": 0
        })
        original = deepcopy(row)
        request = render_session_input(row, created_seq=next_request_sequence(self.history))
        items = self.assemble([*self.history, request])
        self.assertNotEqual(items[0].item_id, items[-1].item_id)
        self.assertNotEqual(items[0].unit_id, items[-1].unit_id)
        self.assertEqual(items[-1].kind, "reminder")
        self.assertEqual(items[-1].origin, "harness:command_completion")
        self.assertEqual(items[-1].trust, "system")
        self.assertIn("task_id=cmd_original status=completed exit_code=0", request.get_text_content())
        self.assertEqual(row, original)

    def test_sequential_multiblock_wakes_rebase_stale_identity_without_mutation(self):
        row = {
            "role": "user",
            "content": [{"type": "text", "text": "Completion"},
                        {"type": "text", "text": "Continue the original request"}],
            SESSION_CONTEXT_META_KEY: {
                "item_id": "session:0:block:0", "created_seq": 0,
                "kind": "reminder", "origin": "harness:command_completion", "trust": "system",
            },
        }
        original = deepcopy(row)
        messages = list(self.history)
        for _ in range(2):
            seq = next_request_sequence(messages)
            request = render_session_input(row, created_seq=seq)
            messages.append(request)
            items = self.assemble(messages)
            self.assertEqual([i.created_seq for i in items[-2:]], [seq, seq + 1])
            self.assertEqual([i.kind for i in items[-2:]], ["reminder", "reminder"])
        self.assertEqual(len({i.item_id for i in items}), len(items))
        self.assertEqual(row, original)

    def test_boundary_replay_keeps_distinct_history_positions(self):
        row = completion_context({
            "task_id": "cmd_original", "status": "completed", "exit_code": 0
        })
        replayed = self.adapter.message_from_session_dict(row, created_seq=1)
        items = self.assemble([*self.history, replayed])
        self.assertEqual([i.item_id for i in items], ["session:0:block:0", "session:1:block:0"])
        self.assertIn("task_id=cmd_original", replayed.get_text_content())


if __name__ == "__main__":
    unittest.main()
