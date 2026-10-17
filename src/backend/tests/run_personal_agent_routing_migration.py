"""Verify upgrade, legacy backfill and rollback in an isolated PostgreSQL database."""
import os
import time
import subprocess
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from core.db.models import UserShadow
from personal_agent.models import PersonalWorkItem, PersonalMessageTask, PersonalMessageInbox
from personal_agent.context import AgentContext

url=make_url(os.environ["DATABASE_URL"]);name="pa_routing_migration_"+str(time.time_ns());test=url.set(database=name)
engine=create_engine(url,isolation_level="AUTOCOMMIT")
with engine.connect() as conn:
    if not conn.execute(text("SELECT 1 FROM pg_database WHERE datname=:name"),{"name":name}).scalar():
        conn.execute(text("CREATE DATABASE "+name))
env={**os.environ,"DATABASE_URL":test.render_as_string(hide_password=False),"JX_EDITION":"ee"}
def migrate(direction, revision):
    subprocess.run(["python","-m","alembic","-c","/app/alembic.ini",direction,revision],env=env,check=True)
migrate("upgrade","personalagent01")
verification=create_engine(test)
UserShadow.__table__.create(verification,checkfirst=True)
with Session(verification) as db:
    db.add(UserShadow(user_id="routing-migration-owner",user_center_id="routing-migration-owner",username="routing-migration-owner"));db.commit()
    svc=AgentContext(db,"routing-migration-owner");agent=svc.identity.ensure();agent=svc.identity.agent()
    db.execute(PersonalWorkItem.__table__.insert().values(id="pa-migration-task",agent_id=agent.id,goal="Legacy goal",request_key="legacy"))
    for role,content in [("user","Legacy goal"),("user","Legacy update"),("assistant","Legacy result")]:
        message=svc.conversation.append(agent,role,content,None)
        message.extra_data={**message.extra_data,"work_item_id":"pa-migration-task"}
        if content=="Legacy goal":db.execute(PersonalWorkItem.__table__.update().where(PersonalWorkItem.id=="pa-migration-task").values(context_seq=message.chat_seq))
    db.commit();chat_id=agent.chat_id
scoped_id=None;single_ids=[];session_receipt_created=False
for direction,revision in [("upgrade","head"),("downgrade","personalagent01"),("upgrade","head")]:
    migrate(direction,revision)
    if direction=="upgrade" and not session_receipt_created:
        with verification.begin() as conn:
            conn.execute(text("INSERT INTO personal_session_operations (id,user_id,target,request_key,operation,args,epoch,status,result,deadline) VALUES ('migration-receipt','routing-migration-owner','cloud','migration-key','chat.create','{}',1,'completed','{\"chat_id\":\"retained\"}',0)"))
        with verification.begin() as conn:
            conn.execute(text("INSERT INTO personal_session_operations (id,user_id,target,request_key,operation,args,epoch,status,deadline) VALUES ('migration-pending','routing-migration-owner','cloud','pending-key','chat.create','{}',1,'queued',extract(epoch from now())+60)"))
        rejected=subprocess.run(["python","-m","alembic","-c","/app/alembic.ini","downgrade","personalagent03"],env=env,capture_output=True,text=True)
        assert rejected.returncode!=0 and "Resolve pending session operations" in rejected.stderr
        with verification.begin() as conn:
            conn.execute(text("UPDATE personal_session_operations SET status='revoked' WHERE id='migration-pending'"))
        session_receipt_created=True
    with verification.connect() as conn:
        assert conn.execute(text("SELECT result->>'chat_id' FROM personal_session_operations WHERE id='migration-receipt'")).scalar()=="retained"
    if direction=="upgrade" and scoped_id is None:
        with Session(verification) as db:
            svc=AgentContext(db,"routing-migration-owner");agent=svc.identity.agent()
            second=PersonalWorkItem(id="pa-migration-second",agent_id=agent.id,goal="Only B",request_key="second");db.add(second);db.flush()
            message=svc.conversation.append(agent,"user","A改分支，B住酒店",None);scoped_id=message.message_id
            second.context_seq=message.chat_seq
            db.add_all([PersonalMessageTask(message_id=scoped_id,work_item_id="pa-migration-task",relation="update",instruction="Only A"),
                PersonalMessageTask(message_id=scoped_id,work_item_id=second.id,relation="goal",instruction="Only B")]);db.commit()
            for relation,instruction in [("pause",""),("update","Only update A")]:
                single=svc.conversation.append(agent,"user","A 和 B 的原始要求",None)
                single.extra_data={**single.extra_data,"work_item_id":"pa-migration-task"}
                db.add(PersonalMessageTask(message_id=single.message_id,work_item_id="pa-migration-task",relation=relation,instruction=instruction))
                single_ids.append((single.message_id,relation,instruction))
            db.commit()
            from personal_agent.tasks.task_threads import sync_thread
            for task in db.scalars(__import__("sqlalchemy").select(PersonalWorkItem)):
                sync_thread(db,task,"routing-migration-owner")
            db.commit()
            db.add(PersonalMessageInbox(message_id=scoped_id,agent_id=agent.id,request_key="pending-check",payload={},status="pending",decision={}));db.commit()
        blocked=subprocess.run(["python","-m","alembic","-c","/app/alembic.ini","downgrade","personalagent01"],env=env,capture_output=True,text=True)
        assert blocked.returncode!=0 and "Resolve pending Personal Agent messages" in blocked.stderr
        with Session(verification) as db:
            row=db.get(PersonalMessageInbox,scoped_id);assert row.status=="pending";row.status="applied";db.commit()
    with verification.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM chat_messages WHERE chat_id=:chat"),{"chat":chat_id}).scalar()==6
        if direction=="upgrade":
            assert set(conn.execute(text("SELECT relation FROM personal_message_tasks WHERE work_item_id='pa-migration-task'")).scalars())=={"goal","update","final","pause"}
            assert set(conn.execute(text("SELECT instruction FROM personal_message_tasks WHERE message_id=:id"),{"id":scoped_id}).scalars())=={"Only A","Only B"}
            for message_id,relation,instruction in single_ids:
                assert list(conn.execute(text("SELECT relation,instruction FROM personal_message_tasks WHERE message_id=:id"),{"id":message_id}))==[(relation,instruction)]
            threads=list(conn.execute(text("SELECT id,chat_id FROM personal_work_items")))
            assert all(chat and chat!=chat_id for _,chat in threads)
            assert len({chat for _,chat in threads})==len(threads)
            branch=conn.execute(text("SELECT content FROM chat_messages WHERE chat_id=(SELECT chat_id FROM personal_work_items WHERE id='pa-migration-task') ORDER BY chat_seq")).scalars().all()
            assert "Legacy goal" in branch and "Legacy update" in branch and "Only A" in branch and "Only update A" in branch
            assert "Only B" not in branch and "A改分支，B住酒店" not in branch
            assert conn.execute(text("SELECT column_default FROM information_schema.columns WHERE table_name='personal_devices' AND column_name='control_enabled'")).scalar()=="0"
            assert conn.execute(text("SELECT to_regclass('personal_message_inbox')")).scalar()=="personal_message_inbox"
            assert conn.execute(text("SELECT column_default FROM information_schema.columns WHERE table_name='personal_devices' AND column_name='sessions_enabled'")).scalar()=="0"
            assert conn.execute(text("SELECT to_regclass('personal_session_operations')")).scalar()=="personal_session_operations"
print("PASS: PostgreSQL upgrade, three legacy links backfilled, rollback retains messages and scoped multi-task instructions, repeat upgrade")
