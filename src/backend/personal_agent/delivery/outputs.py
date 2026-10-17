"""Counts and paginated owner-scoped generated files."""
import mimetypes
from sqlalchemy import select,func,case,or_,and_
from core.db.models import Artifact
from personal_agent.models import PersonalWorkItem

def query(svc):
    agent=svc.identity.agent()
    if not agent:return select(Artifact).where(False)
    tasks=select(PersonalWorkItem.id).where(PersonalWorkItem.agent_id==agent.id)
    sessions=select(PersonalWorkItem.chat_id).where(PersonalWorkItem.agent_id==agent.id)
    return select(Artifact).where(Artifact.user_id==svc.user_id,Artifact.deleted_at.is_(None),
        Artifact.extra_data["source"].as_string()=="ai_generated",
        or_(Artifact.chat_id==agent.chat_id,Artifact.chat_id.in_(sessions),Artifact.extra_data["personal_work_item_id"].as_string().in_(tasks)))

def listing(svc,before=None,limit=50):
    base=query(svc);sub=base.subquery()
    legacy=case(*[(func.lower(sub.c.filename).like("%."+ext),mime) for ext,mime in {
        "png":"image/png","jpg":"image/jpeg","jpeg":"image/jpeg","svg":"image/svg+xml","webp":"image/webp","gif":"image/gif",
        "txt":"text/plain","md":"text/markdown","csv":"text/csv","pdf":"application/pdf","json":"application/json",
        "docx":"application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet","pptx":"application/vnd.openxmlformats-officedocument.presentationml.presentation"}.items()],else_="application/octet-stream")
    mime=case((sub.c.mime_type=="application/octet-stream",legacy),else_=sub.c.mime_type)
    image=mime.like("image/%")
    document=or_(mime.like("text/%"),mime.in_([
        "application/pdf","application/json","application/rtf","application/msword","application/vnd.ms-excel","application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation","application/vnd.oasis.opendocument.text"]))
    stats=svc.db.execute(select(func.count(),func.coalesce(func.sum(case((image,1),else_=0)),0),
        func.coalesce(func.sum(case((document,1),else_=0)),0)).select_from(sub)).one()
    if before:
        cursor=svc.db.scalar(base.where(Artifact.artifact_id==before))
        if cursor is None:raise LookupError("Output not found")
        base=base.where(or_(Artifact.created_at<cursor.created_at,and_(Artifact.created_at==cursor.created_at,Artifact.artifact_id<cursor.artifact_id)))
    rows=list(svc.db.scalars(base.order_by(Artifact.created_at.desc(),Artifact.artifact_id.desc()).limit(limit+1)))
    return {"total":stats[0],"images":stats[1],"documents":stats[2],"other":stats[0]-stats[1]-stats[2],"has_more":len(rows)>limit,"items":[
        {"id":a.artifact_id,"name":(a.extra_data or {}).get("original_filename",a.filename),"mime_type":(mimetypes.guess_type(a.filename)[0] or a.mime_type) if a.mime_type=="application/octet-stream" else a.mime_type,"size_bytes":a.size_bytes,
         "task_id":(a.extra_data or {}).get("personal_work_item_id"),"url":"/api/files/"+a.artifact_id,"created_at":a.created_at.isoformat()}
        for a in rows[:limit]]}
