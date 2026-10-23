"""Build a durable ordinary-chat recipient for completion tests."""
from core.chat.sequencer import ChatSequencer

def source_run(factory):
    with factory() as db:
        run = ChatSequencer(db).accept_main_run(chat_id="c", user_id="u",
            user_content="original request", request_payload={}).run
        run.status = "running"
        run.recovery_snapshot = {"kind":"chat","worker_args":{
            "session_messages":[],"effective_user_message":"original request",
            "raw_user_message":"original request","context":{"chat_mode":"medium","enable_thinking":True}}}
        db.commit()
        return run.run_id
