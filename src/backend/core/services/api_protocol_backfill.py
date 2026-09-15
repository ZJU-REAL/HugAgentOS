"""给存量模型配置补上 ``extra_config.api_protocol``。

新增/编辑模型时由 ``_autofill_api_protocol`` 在保存那刻探测写入；本模块补的是升级前
就已存在、没有这个字段的行。运行期不探测（那是配置期行为，不能挂在请求路径上），所以
启动时一次性补齐，补完落库就不再跑。探不出结论的什么都不写，下次启动再试。
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


def _needs_probe(extra: dict) -> bool:
    """还没探过，或探过但用的是比现在宽松的老判据。

    老判据只验证 ``/responses`` 路由存在，会把「路由在、却不消费工具结果」的上游判成
    可用；那种上游一进智能体循环就卡死。判据变严后，旧结论必须重新验一遍，否则谁也不会
    去纠正它。人工设定的值（没有 source 标记）永远不动。
    """
    from core.llm.providers.protocol_probe import PROBE_VERSION

    if extra.get("api_protocol_source") != "probe":
        return not extra.get("api_protocol")
    return int(extra.get("api_protocol_probe_version") or 1) < PROBE_VERSION


async def backfill_missing_api_protocol() -> int:
    """探测所有还没记录协议（或结论已过期）的 chat 上游并写库，返回写入的条数。"""
    from core.db.engine import SessionLocal
    from core.db.model_repository import list_providers, update_provider
    from core.llm.providers.protocol_probe import PROBE_VERSION, detect_api_protocol
    from core.llm.providers.registry import get_spec

    with SessionLocal() as db:
        pending = []
        for provider in list_providers(db):
            if provider.provider_type != "chat" or not provider.is_active:
                continue
            extra = provider.extra_config or {}
            if not _needs_probe(extra):
                continue
            if not get_spec(getattr(provider, "provider", "") or "").speaks_responses:
                continue
            pending.append(
                (
                    provider.provider_id,
                    provider.model_name,
                    provider.base_url,
                    provider.api_key,
                    dict(extra),
                )
            )

        if not pending:
            return 0

        results = await asyncio.gather(
            *(
                # 超时沿用 detect_api_protocol 自己的默认值：保存时探测与这里补齐是
                # 同一件事，给它第二个数只会让两条路径对同一个上游得出不同结论。
                detect_api_protocol(
                    base_url=base_url,
                    api_key=api_key,
                    model_name=model_name,
                )
                for _, model_name, base_url, api_key, _ in pending
            ),
            return_exceptions=True,
        )

        filled = 0
        for (provider_id, model_name, _, _, extra), result in zip(pending, results):
            if isinstance(result, BaseException) or not getattr(result, "found", False):
                logger.info(
                    "[startup] api_protocol 未能探测 %s：%s",
                    model_name,
                    result if isinstance(result, BaseException) else "; ".join(result.notes),
                )
                continue
            extra["api_protocol"] = result.protocol
            extra["api_protocol_source"] = "probe"
            extra["api_protocol_probe_version"] = PROBE_VERSION
            update_provider(db, provider_id, extra_config=extra)
            filled += 1
            logger.info(
                "[startup] api_protocol 已探明 %s -> %s（%s）",
                model_name,
                result.protocol,
                "; ".join(result.notes),
            )
        return filled
