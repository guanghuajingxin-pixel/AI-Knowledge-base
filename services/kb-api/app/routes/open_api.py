"""Versioned machine API. Machine credentials never grant console access."""
import logging
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from kb_common.database import get_session
from kb_common.platform_auth import authenticate_api_key, require_api_role
from app.routes.knowledge_library import RetrievalTestIn, execute_retrieval
from app.services import masking

router = APIRouter(prefix="/api/openapi/v1", tags=["平台 OpenAPI"])
api_key = APIKeyHeader(name="X-API-Key", scheme_name="PlatformAPIKey", auto_error=False,
                      description="Keycloak 平台应用凭证：client_id:client_secret")


async def retrieval_principal(raw: str | None = Depends(api_key)):
    claims = await authenticate_api_key(raw)
    require_api_role(claims, "knowledge:retrieve")
    return claims


class RetrieveIn(RetrievalTestIn):
    library_ids: list[int] = Field(min_length=1, max_length=24)
    top_k: int = Field(default=8, ge=1, le=50)

    @field_validator("query")
    @classmethod
    def nonempty_query(cls, value):
        if not value.strip():
            raise ValueError("检索词不能为空")
        return value.strip()


class Hit(BaseModel):
    library_id: int
    library_name: str
    document_id: str = ""
    document_title: str = ""
    content: str = ""
    score: float


class RetrieveOut(BaseModel):
    hits: list[Hit]
    total: int
    elapsed_ms: int
    partial: bool
    failed_library_ids: list[int]


@router.post("/knowledge/retrieve", response_model=RetrieveOut,
             summary="检索已开放的知识库", responses={401: {"description": "API Key 无效"},
             403: {"description": "无检索权限或知识库未开放"},
             502: {"description": "检索服务不可用"}, 503: {"description": "统一鉴权或脱敏服务不可用"}})
async def retrieve(body: RetrieveIn, principal=Depends(retrieval_principal),
                   s: AsyncSession = Depends(get_session)):
    result = await execute_retrieval(body, s, public=True)
    failed = [r["library_id"] for r in result["libraries"] if not r["ok"]]
    if len(failed) == len(result["libraries"]):
        raise HTTPException(502, "知识库检索服务暂不可用")
    # Return an explicit field allowlist; engine errors/configuration/URLs stay internal.
    hits = [Hit.model_validate(h) for h in result["hits"]]
    try:
        ctx = await masking.load_mask_context(s, scene="search", user_role="viewer",
            scope_ids={f"library:{i}" for i in body.library_ids}, node="post_output")
        if ctx is not None:
            for hit in hits:
                for field in ("content", "document_title", "library_name"):
                    setattr(hit, field, masking.mask_text(getattr(hit, field), ctx)[0])
    except Exception as exc:
        raise HTTPException(503, "检索结果脱敏服务暂不可用") from exc
    logging.getLogger(__name__).info(
        "platform_retrieve client=%s subject=%s libraries=%s hits=%s failed=%s",
        principal.get("azp"), principal.get("sub"), body.library_ids, len(hits), failed)
    return RetrieveOut(hits=hits, total=len(hits), elapsed_ms=result["elapsed_ms"],
                       partial=bool(failed), failed_library_ids=failed)
