"""Health-check, readiness, and liveness endpoints."""

from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import text

from api.schemas import HealthResponse
from core.config.settings import settings
from core.db.engine import get_db
from core.infra.logging import get_logger
from core.storage import get_storage

logger = get_logger(__name__)

router = APIRouter(tags=["monitoring"])


@router.get("/health", response_model=HealthResponse, summary="健康检查")
async def health_check():
    """
    基础健康检查

    用于监控和负载均衡器的健康检查端点。
    仅检查服务是否响应，不检查依赖。
    """
    return HealthResponse(
        status="healthy",
        service="hugagent",
        timestamp=datetime.now().isoformat(),
    )


@router.get("/ready", summary="就绪探针")
async def readiness_check(db: Session = Depends(get_db)):
    """
    就绪检查（包含依赖检查）

    检查服务及其依赖是否就绪。
    用于 Kubernetes readiness probe。
    """
    checks = {}

    # Check database
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception as e:
        logger.error("database_health_check_failed", error=str(e))
        checks["database"] = False

    # Check storage
    try:
        storage = get_storage()
        checks["storage"] = storage is not None
    except Exception as e:
        logger.error("storage_health_check_failed", error=str(e))
        checks["storage"] = False

    # Check Redis
    redis_url = settings.redis.url
    if redis_url:
        try:
            from core.infra.redis import get_redis
            r = get_redis()
            await r.ping()
            checks["redis"] = True
        except Exception as e:
            logger.error("redis_health_check_failed", error=str(e))
            checks["redis"] = False

    # Check user center
    auth_mode = settings.auth.mode
    if auth_mode != "mock":
        try:
            checks["user_center"] = True
        except Exception as e:
            logger.error("user_center_health_check_failed", error=str(e))
            checks["user_center"] = False
    else:
        checks["user_center"] = True

    all_ready = all(checks.values())
    status_code = 200 if all_ready else 503

    return JSONResponse(
        status_code=status_code,
        content={
            "ready": all_ready,
            "checks": checks,
            "timestamp": datetime.now().isoformat(),
        },
    )


@router.get("/live", summary="存活探针")
async def liveness_check():
    """
    存活检查（简单响应）

    最简单的存活检查，仅返回服务存活状态。
    用于 Kubernetes liveness probe。
    """
    return {"alive": True, "timestamp": datetime.now().isoformat()}
