"""Evolution implementation of the memory settlement boundary."""
from core.db.models import ChatMessage, EvolutionEpisode
from core.evolution.settlement import settlement, settlement_runner, settlement_store
from core.memory.settlement_port import register_memory_settlement


class EvolutionMemorySettlement:
    def report_writes(self, message_id, *, items, failed):
        settlement_runner.report_memory_writes(message_id, items=items, failed=failed)

    def update_entry_text(self, message_id, handle, text):
        return settlement_store.update_entry_text(message_id, handle, text)

    def build_summary(self, message_id, *, session_factory, items, failed):
        settlement_runner.acknowledge_durable_settlement(message_id)
        with session_factory() as db:
            episode = db.query(EvolutionEpisode.episode_id).filter(
                EvolutionEpisode.message_id == message_id
            ).first()
        return settlement.settle_turn(
            message_id=message_id,
            episode_id=str(episode[0]) if episode is not None else "",
            memory_entries=items, memory_failed=failed, memory_enabled=True,
        ).to_dict()

    def write_summary(self, db, message_id, summary):
        # Called inside the outbox acknowledgement transaction. Never commit here.
        message = db.query(ChatMessage).filter(ChatMessage.message_id == message_id).first()
        if message is None:
            raise RuntimeError("assistant message is not durable yet; settlement will retry")
        if not isinstance(summary, dict):
            raise RuntimeError("settlement result is missing its durable summary")
        extra = dict(message.extra_data or {})
        extra[settlement_store.METADATA_KEY] = summary
        message.extra_data = extra


def register() -> None:
    register_memory_settlement(EvolutionMemorySettlement())
