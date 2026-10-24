"""Compose chat projection reducers and checkpoint codecs for the run host."""
from core.runs.events.projection import ProjectionCodec
from core.chat.projection.reducer import fold_projection, new_projection
from core.chat.projection.storage import affected_tools, decode_checkpoint, encode_checkpoint


def chat_projection_codec() -> ProjectionCodec:
    return ProjectionCodec(new_projection, fold_projection, affected_tools, encode_checkpoint, decode_checkpoint)
