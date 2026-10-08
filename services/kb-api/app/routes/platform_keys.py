"""Admin-only platform API Key lifecycle; raw credentials are returned once."""
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import require_role
from app.services import platform_keys
from app.services.audit import write_audit
from kb_common.database import get_session

router = APIRouter(prefix='/api/v1/platform/api-keys', tags=['平台 API Key'])
admin = require_role('super_admin', 'admin')


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)

    @field_validator('name')
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError('应用名称不能为空')
        return value.strip()


class KeyPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None

    @field_validator('name')
    @classmethod
    def nonempty(cls, value):
        return KeyIn.nonempty(value) if value is not None else None


class KeyOut(BaseModel):
    id: str
    client_id: str
    name: str
    enabled: bool
    created_at: str | None
    permissions: list[str]


class KeyCreated(KeyOut):
    raw_key: str


def no_cache(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Pragma'] = 'no-cache'


@router.get('', response_model=list[KeyOut])
async def list_keys(response: Response, u=Depends(admin)):
    no_cache(response)
    return await platform_keys.list_keys()


@router.post('', response_model=KeyCreated, status_code=201)
async def create_key(body: KeyIn, request: Request, response: Response,
                     u=Depends(admin), s: AsyncSession = Depends(get_session)):
    no_cache(response)
    result = await platform_keys.create_key(body.name, str(u.id))
    await write_audit(s, actor_id=u.id, action='platform_key_create', target_type='platform_api_key',
                      target_id=result['id'], detail={'client_id': result['client_id'], 'name': body.name}, request=request)
    return result


@router.get('/{key_id}/secret')
async def reveal_key_secret(key_id: UUID, response: Response, u=Depends(admin)):
    """回显平台 API Key 完整密钥（仅管理员；Keycloak client-secret 明文可读取）。"""
    no_cache(response)
    return await platform_keys.reveal_key(key_id)


@router.patch('/{key_id}', response_model=KeyOut)
async def update_key(key_id: UUID, body: KeyPatch, request: Request, response: Response,
                     u=Depends(admin), s: AsyncSession = Depends(get_session)):
    no_cache(response)
    result = await platform_keys.update_key(key_id, **body.model_dump(exclude_none=True))
    await write_audit(s, actor_id=u.id, action='platform_key_update', target_type='platform_api_key',
                      target_id=str(key_id), detail=body.model_dump(exclude_none=True), request=request)
    return result


@router.delete('/{key_id}', status_code=204)
async def delete_key(key_id: UUID, request: Request, u=Depends(admin), s: AsyncSession = Depends(get_session)):
    await platform_keys.delete_key(key_id)
    await write_audit(s, actor_id=u.id, action='platform_key_delete', target_type='platform_api_key',
                      target_id=str(key_id), request=request)
