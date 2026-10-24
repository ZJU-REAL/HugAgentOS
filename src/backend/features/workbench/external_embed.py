"""Explicit external embedding grants; never fetch or proxy external content."""

from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class EmbeddingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: str = Field(max_length=2048)
    ready: str = Field(pattern="^(integrated|unconfirmed)$")
    allowForms: bool = Field(default=False, strict=True)
    originStorage: bool = Field(default=False, strict=True)


def reject(code="invalid_embedding_policy"):
    raise HTTPException(422, detail={"code": code})


def exact_origin(value):
    try:
        u = urlsplit(value)
        host = u.hostname.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = "[" + host + "]"
        port = u.port
        suffix = (
            ":" + str(port)
            if port is not None and port != {"http": 80, "https": 443}.get(u.scheme)
            else ""
        )
        return urlunsplit((u.scheme, host + suffix, "", "", ""))
    except (AttributeError, ValueError, UnicodeError):
        reject()


def validate_policy(target, embedding, host_origin=None):
    from features.workbench.external import validate_url

    if target["mode"] != "embedded":
        if embedding is not None:
            reject()
        return None
    if embedding is None:
        reject()
    try:
        config = EmbeddingPolicy.model_validate(embedding).model_dump()
    except ValidationError:
        reject()
    validate_url(config["origin"])
    validate_url(target["url"])
    if config["origin"] != exact_origin(config["origin"]):
        reject()
    if exact_origin(target["url"]) != config["origin"]:
        reject()
    # Cookies are hostname-scoped, not port-scoped. A different port or scheme
    # is not sufficient isolation from the host session. Domain cookies are
    # additionally checked by the authentication boundary at launch.
    if host_origin and urlsplit(config["origin"]).hostname == urlsplit(host_origin).hostname:
        reject("embedding_host_origin")
    if config["ready"] == "integrated" and not config["originStorage"]:
        reject()
    return config


def launch_policy(item, host_origin, target=None):
    target = target or item["declaration"]["target"]
    if target.get("kind") != "external" or target.get("mode") != "embedded":
        return {}
    config = validate_policy(target, item.get("embedding"), host_origin)
    return {"embedding": config}
