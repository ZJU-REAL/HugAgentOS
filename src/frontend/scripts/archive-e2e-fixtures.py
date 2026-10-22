"""Create and clean only the marked temporary accounts for Docker archive E2E."""

import io
import json
import secrets
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORK = ROOT / ".scratch/archive-e2e"
LOGIN = WORK / "test-accounts.json"

SEED = """
import json,sys
from core.db.engine import SessionLocal
from core.db.models import UserShadow
from core.auth.local_users import LocalUserService
body=json.load(sys.stdin)
created=[]
with SessionLocal() as db:
    for item in body['users']:
        result=LocalUserService(db).create_by_admin(
            username=item['username'],password=item['password'],
            nickname='压缩包验收账号',actor='archive-e2e',
        )
        if not result.ok:
            print(json.dumps({'users':created,'error':'test_account_creation_failed'}))
            sys.exit(1)
        user=db.get(UserShadow,result.user_id)
        user.extra_data={**(user.extra_data or {}),'archive_e2e_batch':body['batch']}
        db.commit()
        created.append({'username':item['username'],'user_id':result.user_id})
print(json.dumps({'users':created}))
"""

CLEAN = """
import asyncio,json,sys
from core.db.engine import SessionLocal
from core.db.models import UserShadow,Artifact
from core.files.storage import get_storage
from api.app import app
from edition_ee.routes.config_users import delete_user
body=json.load(sys.stdin)
storage=get_storage()
deleted=0
with SessionLocal() as db:
    for item in body['users']:
        user=db.get(UserShadow,item['user_id'])
        if user is None:
            continue
        assert (user.extra_data or {}).get('archive_e2e_batch') == body['batch']
        assert user.username == item['username']
        artifacts=db.query(Artifact).filter(Artifact.user_id==user.user_id).all()
        for artifact in artifacts:
            if artifact.storage_key:
                storage.delete(artifact.storage_key)
        delete_user(user.user_id,db)
        deleted+=1
async def clean_sessions():
    from core.auth.session import get_redis,SESSION_KEY_PREFIX
    ids={item['user_id'] for item in body['users']}
    redis=get_redis()
    count=0
    async for key in redis.scan_iter(match=SESSION_KEY_PREFIX+'*',count=100):
        payload=await redis.get(key)
        if payload and json.loads(payload).get('user_id') in ids:
            count+=await redis.delete(key)
    return count
sessions=asyncio.run(clean_sessions())
print(json.dumps({'deleted_test_users':deleted,'deleted_test_sessions':sessions}))
"""


def execute(code, payload):
    result = subprocess.run(
        ["docker", "exec", "-i", "hugagent-backend", "python", "-c", code],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
    )
    rows = [line for line in result.stdout.splitlines() if line.startswith("{")]
    if not rows:
        raise RuntimeError("Test fixture operation failed; no result was returned")
    data = json.loads(rows[-1])
    return result.returncode, data


def up():
    WORK.mkdir(parents=True, exist_ok=True)
    if LOGIN.exists():
        raise RuntimeError("A previous test account manifest exists; clean it first")
    batch = secrets.token_hex(4)
    users = [
        {"username": f"ar_e2e_{batch}_{index}", "password": secrets.token_urlsafe(24)}
        for index in range(2)
    ]
    body = {"batch": batch, "users": users}
    status, created = execute(SEED, body)
    user_ids = {item["username"]: item["user_id"] for item in created.get("users", [])}
    body["users"] = [
        {**item, "user_id": user_ids[item["username"]]}
        for item in users
        if item["username"] in user_ids
    ]
    LOGIN.write_text(json.dumps(body))
    LOGIN.chmod(0o600)
    if status:
        raise RuntimeError("Temporary account creation failed; clean any created test users")
    from docx import Document
    from reportlab.pdfgen import canvas

    document = Document()
    document.add_paragraph("Archive end-to-end Word preview")
    office = io.BytesIO()
    document.save(office)
    pdf = io.BytesIO()
    c = canvas.Canvas(pdf)
    c.drawString(72, 750, "Archive end-to-end PDF preview")
    c.save()
    members = {
        "资料/说明.txt": "真实 Docker 压缩包端到端验证\n中文文件名正常。".encode(),
        "资料/报告.docx": office.getvalue(),
        "资料/预览.pdf": pdf.getvalue(),
        "资料/图像.png": bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c020000000b49444154789c6364f80f00010501012718e3660000000049454e44ae426082"
        ),
        "资料/页面.html": b"<script>window.archiveExecuted=true</script>",
    }
    with zipfile.ZipFile(WORK / "验收资料.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("空目录/", b"")
        for name, content in members.items():
            archive.writestr(name, content)
    with tarfile.open(WORK / "验收资料.tar.gz", "w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    (WORK / "损坏压缩包.zip").write_bytes(b"invalid zip")
    (WORK / "暂不支持.rar").write_bytes(b"unsupported")
    (WORK / "manifest.json").write_text(
        json.dumps(
            {
                "batch": batch,
                "test_users": [item["user_id"] for item in body["users"]],
                "members": list(members),
            },
            indent=2,
        )
    )
    print(
        "Created two marked test accounts and archive fixtures; credentials are kept in a private temporary file"
    )


def down():
    if not LOGIN.exists():
        print("No temporary test account manifest remains")
        return
    body = json.loads(LOGIN.read_text())
    payload = {
        "batch": body["batch"],
        "users": [
            {"username": item["username"], "user_id": item["user_id"]} for item in body["users"]
        ],
    }
    status, result = execute(CLEAN, payload)
    if status:
        raise RuntimeError("Test fixture cleanup failed")
    LOGIN.unlink()
    print(json.dumps(result))


if sys.argv[1:] == ["up"]:
    up()
elif sys.argv[1:] == ["down"]:
    down()
else:
    raise SystemExit("Usage: archive-e2e-fixtures.py up|down")
