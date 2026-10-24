"""Authenticated application resource routes; shared tickets/stream remain reusable."""

from core.auth.backend import UserContext, require_auth
from core.db.threads import run_in_session
from core.infra.responses import success_response
from core.harness.registry.resource_owners import resource_owner
from core.capabilities.plugins.resources import service
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field


class ResourceOpen(BaseModel):
    model_config = ConfigDict(extra="forbid")
    install_id: str = Field(min_length=1, max_length=160)
    revision: str = Field(min_length=1, max_length=128)
    execution_scope: str = Field(pattern="^(local|cloud)$")
    module_id: str = Field(min_length=1, max_length=64)


class ResourceCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64)
    action: str = Field(min_length=1, max_length=64)
    params: dict = Field(default_factory=dict)


def _owned(db, user_id, key, instance_id, resource_id, *, allow_closed=False):
    row = resource_owner().owned(db, user_id, key, instance_id, resource_id, allow_closed=allow_closed)
    return service.detached(db, row)


def _commandable(db, user_id, key, instance_id, resource_id):
    from core.capabilities.plugins.resources.owners import resolve_installation
    from features.workbench.ui.contract import find_module

    row = resource_owner().owned(db, user_id, key, instance_id, resource_id)
    selected = resolve_installation(db, row, row.user_id)
    module = find_module(selected.ui, row.module_id)
    if not module or "resource.command" not in module.get("grants", []):
        raise HTTPException(403, "resource_command_not_granted")
    return service.detached(db, row)


def routes(route_class, json_write):
    router = APIRouter(
        prefix="/applications/{key}/instances/{instance_id}/resources",
        route_class=route_class,
        dependencies=[Depends(json_write)],
    )

    @router.post("")
    async def create(
        key: str,
        instance_id: str,
        body: ResourceOpen,
        response: Response,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        user: UserContext = Depends(require_auth()),
    ):
        from fastapi import HTTPException

        if not instance_id or len(instance_id) > 128:
            raise HTTPException(422, "invalid_instance_id")
        response.headers["Cache-Control"] = "no-store"
        value = await resource_owner().create(
            str(user.user_id),
            key,
            instance_id,
            **body.model_dump(),
            idempotency_key=idempotency_key,
        )
        return success_response(data={"resource": value})

    @router.post("/{resource_id}/lease")
    async def lease(
        key: str,
        instance_id: str,
        resource_id: str,
        response: Response,
        user: UserContext = Depends(require_auth()),
    ):
        response.headers["Cache-Control"] = "no-store"
        return success_response(
            data={
                "resource": await resource_owner().lease(str(user.user_id), key, instance_id, resource_id)
            }
        )

    @router.delete("/{resource_id}")
    async def close(
        key: str,
        instance_id: str,
        resource_id: str,
        response: Response,
        user: UserContext = Depends(require_auth()),
    ):
        response.headers["Cache-Control"] = "no-store"
        row = await run_in_session(
            _owned, str(user.user_id), key, instance_id, resource_id, allow_closed=True
        )
        if row.status != "closed":
            await service.close(row)
        return success_response(data={"closed": True})

    @router.post("/{resource_id}/command")
    async def command(
        key: str,
        instance_id: str,
        resource_id: str,
        body: ResourceCommand,
        response: Response,
        user: UserContext = Depends(require_auth()),
    ):
        import json

        row = await run_in_session(_commandable, str(user.user_id), key, instance_id, resource_id)
        if len(json.dumps(body.model_dump()).encode()) > 256 * 1024:
            raise HTTPException(413, "resource_command_too_large")
        response.headers["Cache-Control"] = "no-store"
        return success_response(
            data=await service.command(
                row, body.model_dump(), actor="user", connection_id=instance_id
            )
        )

    return router
