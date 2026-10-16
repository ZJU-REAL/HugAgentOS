"""Bounded HTTP transport. Credentials never follow redirects."""

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Redirect refused")


# Plain HTTP never leaves these hosts; "backend" is the in-Compose service name.
INTERNAL = {"127.0.0.1", "localhost", "::1", "backend"}


def enabled(value):
    """On/off setting: anything but 0, false, no or off is on."""
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


class Client:
    def __init__(self, server, token, *, timeout=35):
        parsed = urlsplit(server)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Invalid server URL")
        if (
            parsed.scheme == "http"
            and parsed.hostname not in INTERNAL
            # Private deployments usually serve plain HTTP; HUGAGENT_ALLOW_HTTP=0 requires HTTPS.
            and not enabled(os.environ.get("HUGAGENT_ALLOW_HTTP", "1"))
        ):
            raise ValueError("Cloud endpoint requires HTTPS (HUGAGENT_ALLOW_HTTP=0)")
        if not token or "\n" in token or "\r" in token:
            raise ValueError("Credential required")
        self.server = server.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.opener = build_opener(NoRedirect())

    def request(self, method, path, body=None, *, started=False):
        request = Request(
            self.server + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if started:
                    for _ in range(400):
                        line = response.readline(200000)
                        if not line:
                            break
                        if line.startswith(b"data:"):
                            payload = json.loads(line[5:].strip())
                            if (
                                isinstance(payload, dict)
                                and payload.get("type") == "run_started"
                                and payload.get("run_id")
                            ):
                                return payload
                            if isinstance(payload, dict) and payload.get("error"):
                                raise RuntimeError("Run admission failed")
                    raise RuntimeError(
                        "No run acknowledgement received; check active runs before retry"
                    )
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise RuntimeError("Response exceeds limit")
                if not raw:
                    return {"deleted": True}
                result = json.loads(raw)
                return (
                    result["data"]
                    if isinstance(result, dict) and "code" in result and "data" in result
                    else result
                )
        except HTTPError as exc:
            raise RuntimeError("HugAgentOS HTTP " + str(exc.code) + "; request refused") from None
        except (URLError, TimeoutError, OSError):
            raise RuntimeError(
                "Connection unavailable; check operation status before retry"
            ) from None

    def execute(self, operation, args):
        from .contract import endpoint

        method, path, body = endpoint(operation, args)
        return self.request(method, path, body, started=operation == "chat.send")
