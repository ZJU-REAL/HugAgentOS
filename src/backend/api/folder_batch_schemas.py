"""Bounded directory-manifest input shared by personal and enterprise routes."""

from typing import Annotated

from pydantic import BaseModel, Field


class FolderBatchBody(BaseModel):
    paths: list[Annotated[str, Field(min_length=1, max_length=2047)]] = Field(
        min_length=1, max_length=200
    )
    parent_folder_id: str | None = Field(default=None, max_length=64)
