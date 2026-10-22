"""Compose desktop capability routes in their original security and API order."""
from fastapi import APIRouter
from features.desktop.api import capability_auth, capability_workcopies, capability_manifests, capability_tools, capability_model_gateway, capability_mcp_gateway, capability_bridge

router = APIRouter()
router.include_router(capability_auth.router)
router.include_router(capability_workcopies.router)
router.include_router(capability_manifests.router)
router.include_router(capability_tools.router)
router.include_router(capability_model_gateway.router)
router.include_router(capability_mcp_gateway.router)
router.include_router(capability_bridge.router)
