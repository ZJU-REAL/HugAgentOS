"""Validated search input; no legacy argument coercion."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Topic = Literal["general", "news", "finance"]
Depth = Literal["basic", "advanced", "fast", "ultra-fast"]
ResultLimit = Annotated[int, Field(strict=True, ge=1, le=8)]
Query = Annotated[str, Field(strict=True, max_length=2000)]


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: Query = ""
    queries: list[Query] | None = None
    max_results: ResultLimit = 5
    topic: Topic = "general"
    search_depth: Depth = "advanced"
    include_raw_content: bool = False

    @model_validator(mode="after")
    def validate_queries(self):
        self.query = self.query.strip()
        if self.queries is not None:
            if self.query:
                raise ValueError("Use query or queries, not both")
            self.queries = list(dict.fromkeys(q.strip() for q in self.queries if q.strip()))
            if not 1 <= len(self.queries) <= 4:
                raise ValueError("queries must contain 1–4 distinct non-empty queries")
        elif not self.query:
            raise ValueError("query or queries is required")
        return self

    def query_list(self) -> list[str]:
        return self.queries if self.queries is not None else [self.query]
