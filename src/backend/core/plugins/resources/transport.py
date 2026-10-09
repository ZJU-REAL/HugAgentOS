"""A resource connection owns its HTTP pool and releases it on disconnect."""
import httpx
from core.infra.http_errors import response_error
from fastapi import HTTPException


class RuntimeTransport:
    def __init__(self, target):
        self.url = target["url"]
        self.client = httpx.AsyncClient(timeout=40, trust_env=False, headers=target["headers"])

    async def __aenter__(self):
        await self.client.__aenter__()
        return self

    async def __aexit__(self, *args):
        await self.client.__aexit__(*args)

    async def request(self, path, body=None):
        try:
            response = (await self.client.get(self.url + path) if body is None
                        else await self.client.post(self.url + path, json=body))
            if response.status_code >= 400:
                raise HTTPException(response.status_code,
                                    response_error(response, f"runtime_http_{response.status_code}"))
            try:
                return response.json()
            except ValueError as error:
                raise HTTPException(502, "runtime_invalid_json") from error
        except httpx.HTTPError as exc:
            raise HTTPException(503, "runtime_disconnected_result_unknown") from exc
