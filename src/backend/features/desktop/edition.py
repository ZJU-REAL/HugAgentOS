"""Community desktop approval controls."""
from typing import Literal
from pydantic import BaseModel
from fastapi import Depends
from core.auth.backend import UserContext, get_current_user
from core.infra.responses import success_response
from features.desktop.local import grants

def register_local_routes(router, require_local):
    class ApprovalBody(BaseModel):
        mode: Literal["strict", "standard", "full"]


    @router.get("/approval-mode", summary="获取当前审批档（对话框权限栏）")
    async def get_approval_mode(_user: UserContext = Depends(get_current_user)):
        require_local()
        return success_response(data={"mode": grants.get_approval_mode()})


    @router.put("/approval-mode", summary="设置审批档")
    async def set_approval_mode(body: ApprovalBody, _user: UserContext = Depends(get_current_user)):
        require_local()
        return success_response(data={"mode": grants.set_approval_mode(body.mode)}, message="已切换权限档")
