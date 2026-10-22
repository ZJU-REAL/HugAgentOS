"""OpenSandbox SDK connection and endpoint configuration."""

from __future__ import annotations

from typing import Any

from core.config.settings import settings
from core.sandbox.providers.opensandbox.runtime import logger


class _OpenSandboxConnectionMixin:
    def _install_endpoint_fastpath(self) -> None:
        """Skip the OpenSandbox server's ``GET /endpoints/{port}`` call.

        Measured, that endpoint takes a fixed ~3.3s (hard server-side latency,
        independent of whether the sandbox is ready), while the proxy endpoint it
        returns is a **deterministic string** ``{host}/sandboxes/{id}/proxy/{port}``
        that can be constructed locally. ``Sandbox.create`` internally calls it once
        each for the execd + egress ports → constructing locally saves ~6.6s of cold
        start (measured: create drops from ~8s to ~1s). The execd endpoint is still
        subsequently switched to a direct connection by ``_repoint_execd_direct`` (if
        reachable); we don't use egress.

        Only enabled in **insecure mode (api_key empty)**: with an api_key the server
        may stuff routing/auth headers into the endpoint that local construction
        cannot replicate → fall back to the SDK default (slow but correct).

        Idempotent: patched only once, tracked by a marker on the SDK class. A patch
        failure is only logged and falls back to the SDK default, without affecting
        functionality.
        """
        if not settings.sandbox.opensandbox_endpoint_fastpath_enabled:
            return
        if self._api_key:
            logger.info(
                "[opensandbox] endpoint fastpath disabled (api_key set; cannot "
                "replicate server-side route headers)"
            )
            return
        try:
            from opensandbox.adapters import sandboxes_adapter as _sa
            from opensandbox.models.sandboxes import SandboxEndpoint as _SE

            if getattr(_sa.SandboxesAdapter, "_jx_endpoint_fastpath", False):
                return
            original_get_endpoint = _sa.SandboxesAdapter.get_sandbox_endpoint

            async def _fast_get_sandbox_endpoint(
                _self: Any,
                sandbox_id: str,
                port: int,
                use_server_proxy: bool = False,
            ) -> Any:
                # This SDK hook is global: authenticated controllers must keep their
                # own endpoint resolution, including the server's routing headers.
                config = _self.connection_config
                if config.get_api_key():
                    return await original_get_endpoint(_self, sandbox_id, port, use_server_proxy)
                host = config.domain.split("://", 1)[-1].rstrip("/")
                return _SE(
                    endpoint=f"{host}/sandboxes/{sandbox_id}/proxy/{port}",
                    headers={},
                )

            _sa.SandboxesAdapter.get_sandbox_endpoint = _fast_get_sandbox_endpoint
            _sa.SandboxesAdapter._jx_endpoint_fastpath = True
            logger.info(
                "[opensandbox] endpoint fastpath installed "
                "(skip server /endpoints ~3.3s x2 per create)"
            )
        except (
            Exception
        ) as exc:  # noqa: BLE001 — safe fallback to the default if the SDK structure changes
            logger.warning(
                "[opensandbox] endpoint fastpath install failed (using SDK default): %s",
                exc,
            )

    def _get_http_client(self) -> Any:
        """Lazy-init the shared httpx client. Many high-frequency small requests share one connection pool, saving TLS handshakes."""
        import httpx

        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=self._request_timeout_s,
                headers={"OPEN-SANDBOX-API-KEY": self._api_key} if self._api_key else {},
            )
        return self._http_client

    def _make_config(self) -> Any:
        from datetime import timedelta as _td

        from opensandbox.config import ConnectionConfig

        return ConnectionConfig(
            domain=self._domain,
            api_key=self._api_key or None,
            use_server_proxy=True,
            request_timeout=_td(seconds=self._request_timeout_s),
        )
