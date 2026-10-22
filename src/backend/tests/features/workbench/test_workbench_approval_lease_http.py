"""Actual session/private ZIP boundary; no authorization overrides."""
import asyncio
from urllib.parse import quote
import pytest
from core.auth import session
from core.config.settings import settings
from core.db.engine import SessionLocal
from core.db.models import ContentBlock
from features.workbench.capabilities import record_id
from tests.features.workbench.test_workbench_application_config_http import HOST, boundary, package

A="lease_instance_alpha_123";B="lease_instance_beta_123"
def installed(client,payload=None):
    result=client.post("/v1/plugins/import",files={"file":("lease.zip",payload or package(name="lease-boundary"),"application/zip")})
    assert result.status_code==201,result.text
    install_id=result.json()["data"]["install_id"]
    item=next(i for i in client.get("/v1/workbench/catalog").json()["data"]["items"]
              if i.get("binding",{}).get("installId")==install_id)
    root="/v1/workbench/applications/"+quote(item["applicationKey"],safe="")
    body={"revision":item["binding"]["packageRevision"],"instanceId":A}
    for instance in (A,B):
        response=client.post(root+"/revision-leases",headers=HOST,json={**body,"instanceId":instance})
        assert response.status_code==200,response.text
    return item,root,body

def approval(client,root,body,capabilities=None):
    response=client.post(root+"/capabilities/approval",headers=HOST,json={**body,"capabilities":capabilities or ["data.query"]})
    assert response.status_code==200,response.text
    return response.json()["data"]["approvalId"]

def check(client,root,body,token):
    return client.post(root+"/capabilities/check",headers=HOST,
                       json={**body,"approvalId":token,"capability":"data.query"})

def test_closed_current_lease_denies_token_and_revoke_is_persisted_without_delete(boundary):
    clients,_,_=boundary;client=clients["owner"];item,root,body=installed(client)
    token=approval(client,root,body);sibling=approval(client,root,{**body,"instanceId":B})
    assert check(client,root,body,token).status_code==200
    response=client.post(root+"/revision-leases/close",headers=HOST,json=body)
    assert response.status_code==200,response.text
    response=check(client,root,body,token)
    assert response.status_code in (403,410),response.text
    with SessionLocal() as db:assert db.get(ContentBlock,record_id(token)).payload["revoked"]
    assert check(client,root,{**body,"instanceId":B},sibling).status_code==200

@pytest.mark.parametrize("supplied",[None,B])
def test_bound_approval_does_not_allow_missing_or_other_instance(boundary,supplied):
    clients,_,_=boundary;client=clients["owner"];_,root,body=installed(client)
    token=approval(client,root,body);request=dict(body)
    if supplied is None:request.pop("instanceId")
    else:request["instanceId"]=supplied
    response=check(client,root,request,token)
    assert response.status_code==403,response.text
    assert check(client,root,body,token).status_code==200

def test_logout_relogin_cannot_reuse_previous_bound_approval(boundary):
    clients,_,_=boundary;client=clients["owner"];_,root,body=installed(client)
    token=approval(client,root,body)
    assert check(client,root,body,token).status_code==200
    assert client.post("/v1/auth/logout",json={}).status_code==200
    new_session=asyncio.run(session.create_session({"user_id":"owner","username":"owner"}))
    client.cookies.set(settings.session.cookie_name,new_session)
    response=check(client,root,body,token)
    assert response.status_code in (403,410),response.text
    with SessionLocal() as db:assert db.get(ContentBlock,record_id(token)).payload["revoked"]

def test_ui3_approval_requires_a_registered_active_instance(boundary):
    clients,_,_=boundary;client=clients["owner"];_,root,body=installed(client)
    for instance in (None,"never_registered_123"):
        request={"revision":body["revision"],"capabilities":["data.query"]}
        if instance is not None:request["instanceId"]=instance
        result=client.post(root+"/capabilities/approval",headers=HOST,json=request)
        assert result.status_code in (403,410),result.text

def test_retained_old_revision_requires_the_same_live_lease(boundary):
    import io,json,zipfile
    clients,_,_=boundary;client=clients["owner"];item,root,body=installed(client)
    token=approval(client,root,body)
    archive=zipfile.ZipFile(io.BytesIO(package(name="lease-boundary")))
    manifest=json.loads(archive.read("plugin.json"));manifest["version"]="2.0.0"
    output=io.BytesIO()
    with zipfile.ZipFile(output,"w") as updated:
        updated.writestr("plugin.json",json.dumps(manifest));updated.writestr("web/index.html","New retained bytes")
    response=client.post("/v1/plugins/import",files={"file":("upgrade.zip",output.getvalue(),"application/zip")})
    assert response.status_code==201,response.text
    current=next(i for i in client.get("/v1/workbench/catalog").json()["data"]["items"]
                 if i["applicationKey"]==item["applicationKey"])
    assert current["binding"]["packageRevision"]!=body["revision"]
    assert check(client,root,body,token).status_code==200
    assert client.post(root+"/revision-leases/close",headers=HOST,json=body).status_code==200
    assert check(client,root,body,token).status_code in (403,410)

@pytest.mark.parametrize("field",["userId","applicationKey","revision","instanceId","installId","bindingToken","expiresAt"])
def test_current_lease_full_identity_and_expiration_are_authoritative(boundary,field):
    from features.workbench.plugin_revisions import lease_id
    clients,_,_=boundary;client=clients["owner"];item,root,body=installed(client)
    token=approval(client,root,body)
    with SessionLocal() as db:
        row=db.get(ContentBlock,lease_id("owner",item["applicationKey"],body["revision"],A))
        row.payload={**row.payload,field:0 if field=="expiresAt" else "wrong-value"}
        db.commit()
    response=check(client,root,body,token)
    assert response.status_code==410,response.text

def test_inflight_owned_http_query_checks_closed_instance_before_return(boundary,monkeypatch):
    import threading,asyncio
    from concurrent.futures import ThreadPoolExecutor
    from tests.features.workbench.test_workbench_application_config_http import configure
    from features.workbench.api import workbench_capabilities as route
    clients,upstream,calls=boundary;client=clients["owner"];item,root,body=installed(client)
    assert configure(clients["admin"],item,{"endpoint":upstream,"token":"owned-test-secret"}).status_code==200
    token=approval(client,root,body,["data.query","data_source:records"])
    entered,finish=threading.Event(),threading.Event();original=route.call_data_source
    async def controlled_response(*args,**kwargs):
        result=await original(*args,**kwargs)
        entered.set()
        await asyncio.to_thread(finish.wait,5)
        return result
    monkeypatch.setattr(route,"call_data_source",controlled_response)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(client.post,root+"/capabilities/data/query",headers=HOST,
                            json={**body,"approvalId":token,"sourceId":"records","params":{}})
        try:
            assert entered.wait(5)
            assert client.post(root+"/revision-leases/close",headers=HOST,json=body).status_code==200
        finally:finish.set()
        response=pending.result()
    assert response.status_code in (403,410),response.text
    assert len(calls)==1

def test_private_file_upload_download_bind_instance_and_poststorage_close_denies(boundary,monkeypatch):
    import io,json,zipfile,threading,os
    from pathlib import Path
    from concurrent.futures import ThreadPoolExecutor
    from core.files.storage import get_storage
    clients,_,_=boundary;client=clients["owner"]
    original=zipfile.ZipFile(io.BytesIO(package(name="lease-files")))
    manifest=json.loads(original.read("plugin.json"))
    manifest["extensions"]["org.hugagent"]["ui"]["contributes"]["modules"][0]["grants"]=["file.save"]
    output=io.BytesIO()
    with zipfile.ZipFile(output,"w") as archive:
        archive.writestr("plugin.json",json.dumps(manifest));archive.writestr("web/index.html","File module")
    _,root,body=installed(client,output.getvalue());token=approval(client,root,body,["file.save"])
    path=root+"/capabilities"
    for instance in (None,B):
        data={"revision":body["revision"],"approvalId":token}
        if instance:data["instanceId"]=instance
        result=client.post(path+"/files/save",headers=HOST,data=data,files={"file":("denied.txt",b"denied","text/plain")})
        assert result.status_code==403,result.text
    saved=client.post(path+"/files/save",headers=HOST,data={**body,"approvalId":token},
                      files={"file":("private.txt",b"isolated private bytes","text/plain")})
    assert saved.status_code==200,saved.text
    file_id=saved.json()["data"]["fileId"]
    for instance in (None,B):
        request={"revision":body["revision"],"approvalId":token,"fileId":file_id}
        if instance:request["instanceId"]=instance
        assert client.post(path+"/files/resource",headers=HOST,json=request).status_code==403
    selected=client.post(path+"/files/resource",headers=HOST,json={**body,"approvalId":token,"fileId":file_id})
    assert selected.status_code==200,selected.text
    url=selected.json()["data"]["resourceUrl"];assert "instanceId="+A in url
    headers={**HOST,"X-Workbench-Approval":token}
    assert client.get(url,headers=headers).content==b"isolated private bytes"
    for instance in (None,B):
        params={"revision":body["revision"]}
        if instance:params["instanceId"]=instance
        assert client.get(path+"/files/"+file_id,headers=headers,params=params).status_code==403
    entered,finish=threading.Event(),threading.Event()
    storage=get_storage();download=storage.download
    def controlled_download(*args,**kwargs):
        result=download(*args,**kwargs);entered.set();assert finish.wait(5);return result
    monkeypatch.setattr(storage,"download",controlled_download)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(client.get,url,headers=headers)
        try:
            assert entered.wait(5)
            assert client.post(root+"/revision-leases/close",headers=HOST,json=body).status_code==200
        finally:finish.set()
        response=pending.result()
    assert response.status_code in (403,410),response.text
    assert b"isolated private bytes" not in response.content
    assert not list((Path(os.environ["STORAGE_PATH"])/".workbench-resources").iterdir())

def test_empty_json_get_configuration_and_query_only_delete_are_legal(boundary):
    from tests.features.workbench.test_workbench_application_config_http import ROOT
    clients,_,_=boundary;client=clients["owner"];item,root,body=installed(client)
    token=approval(client,root,body)
    headers={**HOST,"Content-Type":"application/json"}
    result=clients["admin"].get(ROOT+"/configurable",headers=headers)
    assert result.status_code==200,result.text
    assert any(row["applicationKey"]==item["applicationKey"] for row in result.json()["data"]["items"])
    result=client.delete(root+"/capabilities/approval/"+token,headers=headers,
                         params={"revision":body["revision"]})
    assert result.status_code==200,result.text
    assert check(client,root,body,token).status_code==403

def test_json_budget_origin_and_required_write_body_remain_enforced(boundary):
    from tests.features.workbench.test_workbench_application_config_http import ROOT
    clients,_,_=boundary;client=clients["owner"];_,root,body=installed(client)
    headers={**HOST,"Content-Type":"application/json"}
    response=client.post(root+"/capabilities/approval",headers=headers)
    assert response.status_code==422,response.text
    response=client.post(root+"/capabilities/approval",headers=headers,content="{")
    assert response.status_code==422 and response.json()["detail"]["code"]=="invalid_json"
    response=client.post(root+"/capabilities/approval",headers=headers,content=" "*74000)
    assert response.status_code==413,response.text
    response=clients["admin"].request("GET",ROOT+"/configurable",headers=headers,content=" "*74000)
    assert response.status_code==413,response.text
    import json
    nested={}
    for _ in range(20):nested={"next":nested}
    response=client.post(root+"/capabilities/approval",headers=headers,content=json.dumps(nested))
    assert response.status_code==413,response.text
    response=clients["admin"].get(ROOT+"/configurable",headers={"Content-Type":"application/json","Origin":"null","Sec-Fetch-Site":"cross-site"})
    assert response.status_code==403,response.text

def test_empty_json_get_configuration_query_only_delete_existing_approval(boundary):
    clients,_,_=boundary;client=clients["owner"];_,root,body=installed(client)
    token=approval(client,root,body)
    response=client.delete(root+"/capabilities/approval/"+token,
                           headers={**HOST,"Content-Type":"application/json"},
                           params={"revision":body["revision"]})
    assert response.status_code==200,response.text
    with SessionLocal() as db:assert db.get(ContentBlock,record_id(token)).payload["revoked"]
