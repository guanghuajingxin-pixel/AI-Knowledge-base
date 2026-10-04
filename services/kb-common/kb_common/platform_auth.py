"""Platform machine credentials, centrally issued/revoked by Keycloak.

X-API-Key is client_id:client_secret. No local copy of secrets or token cache:
each request checks the current client credentials with the identity provider.
"""
import httpx
from fastapi import HTTPException

from kb_common.config import get_settings
from kb_common.oidc import enabled, oidc_config, verify_access_token


async def authenticate_api_key(raw: str | None) -> dict:
    if not enabled():
        raise HTTPException(503, "平台 OpenAPI 需要启用 Keycloak 统一鉴权")
    cfg = get_settings()
    oidc_config()
    client_id, separator, secret = (raw or "").partition(":")
    if not separator or not secret or len(raw) > 4096 or client_id not in cfg.oidc_api_clients:
        raise HTTPException(401, "无效的平台 API Key")
    url = cfg.oidc_token_url or f"{cfg.oidc_issuer.rstrip('/')}/protocol/openid-connect/token"
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
            response = await client.post(url, data={
                "grant_type": "client_credentials", "client_id": client_id,
                "client_secret": secret,
            })
        if response.status_code in (400, 401, 403):
            raise HTTPException(401, "平台 API Key 无效或已停用")
        response.raise_for_status()
        token = response.json()["access_token"]
        if not isinstance(token, str):
            raise ValueError("Invalid access token")
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(503, "统一鉴权服务暂不可用") from exc
    return await verify_access_token(token, allowed_clients=(client_id,))


def require_api_role(claims: dict, role: str) -> None:
    resources = claims.get("resource_access")
    access = resources.get(get_settings().oidc_audience) if isinstance(resources, dict) else None
    roles = access.get("roles") if isinstance(access, dict) else None
    if not isinstance(roles, list) or role not in roles:
        raise HTTPException(403, "该平台应用未获授知识库检索权限")
