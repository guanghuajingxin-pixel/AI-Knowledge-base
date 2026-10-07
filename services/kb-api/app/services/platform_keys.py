"""Keycloak owns platform application credentials; this service stores no secrets."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException

from kb_common.config import get_settings
from kb_common.oidc import enabled, oidc_config

MARKER = 'knowledge-platform-api'
PREFIX = 'platform-api-'
logger = logging.getLogger(__name__)


def configuration():
    cfg = get_settings()
    if not enabled():
        raise HTTPException(503, '请先启用 Keycloak 统一认证后再签发平台 API Key')
    oidc_config()
    if not cfg.oidc_admin_client_id or not cfg.oidc_admin_client_secret.get_secret_value():
        raise HTTPException(503, '未配置平台密钥管理服务账号，请按 API 调用说明完成身份中心接入')
    return cfg


class KeycloakAdmin:
    def __init__(self, client, base):
        self.client, self.base = client, base

    async def call(self, method, path, **kwargs):
        try:
            response = await self.client.request(method, f'{self.base}/{path}', **kwargs)
            if response.status_code == 404:
                raise HTTPException(404, '平台应用或检索角色不存在，请检查身份中心配置')
            if response.status_code in (401, 403):
                raise HTTPException(503, '密钥管理服务账号权限不足，请检查身份中心授权')
            if response.status_code == 409:
                raise HTTPException(409, '应用已存在，请刷新列表')
            response.raise_for_status()
            return response.json() if response.content else None
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(503, '身份中心暂不可用，请稍后重试') from exc


@asynccontextmanager
async def admin_session():
    cfg = configuration()
    issuer = cfg.oidc_issuer.rstrip('/')
    root, realm = issuer.rsplit('/realms/', 1)
    base = cfg.oidc_admin_url.rstrip('/') or f'{root}/admin/realms/{realm}'
    token_url = cfg.oidc_token_url or f'{issuer}/protocol/openid-connect/token'
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        try:
            response = await client.post(token_url, data={
                'grant_type': 'client_credentials', 'client_id': cfg.oidc_admin_client_id,
                'client_secret': cfg.oidc_admin_client_secret.get_secret_value(),
            })
            response.raise_for_status()
            token = response.json()['access_token']
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise HTTPException(503, '密钥管理服务账号无法认证，请检查身份中心配置') from exc
        client.headers['Authorization'] = f'Bearer {token}'
        yield KeycloakAdmin(client, base)


def managed(row):
    return (row.get('clientId', '').startswith(PREFIX)
            and row.get('attributes', {}).get('platform.api.managed') == MARKER)


def public(row):
    attrs = row.get('attributes') or {}
    return {'id': row['id'], 'client_id': row['clientId'], 'name': row.get('name') or row['clientId'],
            'enabled': bool(row.get('enabled')), 'created_at': attrs.get('platform.api.created_at'),
            'permissions': ['knowledge:retrieve']}


async def get_managed(admin, key_id: UUID):
    row = await admin.call('GET', f'clients/{key_id}')
    if not managed(row):
        raise HTTPException(404, '未找到平台 API Key')
    return row


async def list_keys():
    async with admin_session() as admin:
        rows, offset = [], 0
        while True:
            batch = await admin.call('GET', 'clients', params={
                'clientId': PREFIX, 'search': 'true', 'first': offset, 'max': 100,
            })
            rows.extend(public(row) for row in batch if managed(row))
            if len(batch) < 100:
                break
            offset += 100
    return sorted(rows, key=lambda row: row['created_at'] or '', reverse=True)


async def create_key(name: str, actor_id: str):
    cfg = configuration()
    async with admin_session() as admin:
        resources = await admin.call('GET', 'clients', params={'clientId': cfg.oidc_audience, 'exact': 'true'})
        if len(resources) != 1:
            raise HTTPException(503, '未找到知识治理资源客户端，请检查身份中心配置')
        resource_id = resources[0]['id']
        role = await admin.call('GET', f'clients/{resource_id}/roles/knowledge:retrieve')
        client_id = PREFIX + uuid4().hex
        payload = {
            'clientId': client_id, 'name': name, 'protocol': 'openid-connect',
            'enabled': False, 'publicClient': False, 'clientAuthenticatorType': 'client-secret',
            'serviceAccountsEnabled': True, 'standardFlowEnabled': False,
            'implicitFlowEnabled': False, 'directAccessGrantsEnabled': False,
            'fullScopeAllowed': False, 'defaultClientScopes': ['roles'],
            'attributes': {'platform.api.managed': MARKER,
                           'platform.api.created_at': datetime.now(timezone.utc).isoformat(),
                           'platform.api.created_by': actor_id},
            'protocolMappers': [
                {'name': 'platform-resource-audience', 'protocol': 'openid-connect',
                 'protocolMapper': 'oidc-audience-mapper', 'config': {
                     'included.client.audience': cfg.oidc_audience, 'access.token.claim': 'true', 'id.token.claim': 'false'}},
                {'name': 'platform-api-marker', 'protocol': 'openid-connect',
                 'protocolMapper': 'oidc-hardcoded-claim-mapper', 'config': {
                     'claim.name': 'platform_api', 'claim.value': 'true', 'jsonType.label': 'boolean',
                     'access.token.claim': 'true', 'id.token.claim': 'false'}},
            ],
        }
        key_id = None
        try:
            await admin.call('POST', 'clients', json=payload)
            rows = await admin.call('GET', 'clients', params={'clientId': client_id, 'exact': 'true'})
            key_id = rows[0]['id']
            service_user = await admin.call('GET', f'clients/{key_id}/service-account-user')
            await admin.call('POST', f'users/{service_user["id"]}/role-mappings/clients/{resource_id}', json=[role])
            await admin.call('POST', f'clients/{key_id}/scope-mappings/clients/{resource_id}', json=[role])
            secret = (await admin.call('GET', f'clients/{key_id}/client-secret'))['value']
            await admin.call('PUT', f'clients/{key_id}', json={'enabled': True})
            return {**public({**payload, 'id': key_id, 'enabled': True}), 'raw_key': f'{client_id}:{secret}'}
        except Exception:
            # Created disabled; a timeout before resolving its ID cannot expose a usable key.
            if key_id:
                try:
                    await admin.call('DELETE', f'clients/{key_id}')
                except Exception:
                    logger.error('清理未完成签发的平台应用失败 client_id=%s', client_id)
            raise


async def update_key(key_id: UUID, *, name=None, enabled=None):
    async with admin_session() as admin:
        row = await get_managed(admin, key_id)
        changes = {}
        if name is not None:
            changes['name'] = name
        if enabled is not None:
            changes['enabled'] = enabled
        await admin.call('PUT', f'clients/{key_id}', json=changes)
        return public({**row, **changes})


async def delete_key(key_id: UUID):
    async with admin_session() as admin:
        await get_managed(admin, key_id)
        await admin.call('DELETE', f'clients/{key_id}')
