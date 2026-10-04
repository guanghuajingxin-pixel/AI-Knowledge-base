"""Keycloak access-token validation and explicit external identity mapping.

Only trust configured issuer/JWKS and kb-api client roles. Never link by email/name.
Both kb-api and faq-service use this module so authorization cannot drift.
"""
import asyncio
import logging
import secrets
import time
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from kb_common.config import get_settings
from kb_common.database import short_session
from kb_common.models import User, UserIdentity
from kb_common.security import decode_jwt, hash_password

ROLE_ORDER = ("super_admin", "admin", "editor", "viewer")
_keys: dict[str, tuple[float, list[dict]]] = {}
_lock = asyncio.Lock()


def enabled() -> bool:
    return get_settings().auth_provider == "keycloak"


def require_local_auth() -> None:
    if enabled():
        raise HTTPException(409, "账号已由统一身份中心管理，请使用统一登录或前往账号中心操作")


def oidc_config() -> dict:
    cfg = get_settings()
    if not enabled():
        return {"provider": "local"}
    issuer = cfg.oidc_issuer.rstrip("/")
    parsed = urlparse(issuer)
    if not parsed.netloc or parsed.scheme not in ("http", "https") or "/realms/" not in parsed.path:
        raise HTTPException(503, "统一登录配置不完整，请联系管理员")
    base, realm = issuer.rsplit("/realms/", 1)
    if not realm or "/" in realm or parsed.query or parsed.fragment:
        raise HTTPException(503, "统一登录 Realm 配置无效")
    return {"provider": "keycloak", "url": base, "realm": realm,
            "portal_client_id": cfg.oidc_portal_client_id,
            "web_client_id": cfg.oidc_web_client_id,
            "account_url": f"{issuer}/account/",
            "admin_url": f"{base}/admin/{realm}/console/"}


def role_from_claims(claims: dict) -> str:
    resources = claims.get("resource_access")
    access = resources.get(get_settings().oidc_audience) if isinstance(resources, dict) else None
    roles = access.get("roles", []) if isinstance(access, dict) else []
    if not isinstance(roles, list):
        raise HTTPException(403, "统一账号尚未获授知识治理权限，请联系管理员")
    for role in ROLE_ORDER:
        if role in roles:
            return role
    raise HTTPException(403, "统一账号尚未获授知识治理权限，请联系管理员")


async def verify_access_token(token: str, *, allowed_clients: tuple[str, ...] | None = None) -> dict:
    cfg = get_settings()
    oidc_config()  # fail closed for incomplete configuration
    issuer = cfg.oidc_issuer.rstrip("/")
    # The optional internal URL is administrator-configured, never derived from a token.
    jwks_url = cfg.oidc_jwks_url or f"{issuer}/protocol/openid-connect/certs"
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not header.get("kid"):
            raise JWTError("Unsupported signing key")
        async with _lock:
            cached_at, keys = _keys.get(jwks_url, (0, []))
            key = next((key for key in keys if key.get("kid") == header["kid"]), None)
            # Refresh rotated keys, but cap unknown-kid traffic to one network request / 10s.
            if time.monotonic() - cached_at > 300 or (key is None and time.monotonic() - cached_at > 10):
                async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
                    response = await client.get(jwks_url)
                    response.raise_for_status()
                    keys = response.json()["keys"]
                _keys[jwks_url] = (time.monotonic(), keys)
                key = next((key for key in keys if key.get("kid") == header["kid"]), None)
        if not key or key.get("kty") != "RSA":
            raise JWTError("Unknown signing key")
        claims = jwt.decode(token, key, algorithms=["RS256"], issuer=issuer,
                            audience=cfg.oidc_audience,
                            options={"require_exp": True, "require_iat": True,
                                     "require_sub": True, "require_aud": True})
        # Reject ID tokens and access tokens minted for other clients in the same realm.
        if claims.get("typ") != "Bearer" or claims.get("azp") not in (allowed_clients if allowed_clients is not None else (
            cfg.oidc_portal_client_id, cfg.oidc_web_client_id
        )):
            raise JWTError("Unexpected token type or authorized party")
        return claims
    except (httpx.HTTPError, KeyError, TypeError, ValueError, JWTError) as exc:
        logging.getLogger(__name__).info("OIDC validation failed: %s", type(exc).__name__)
        raise HTTPException(401, "统一登录凭证无效或已过期，请重新登录") from exc


async def resolve_oidc_user(claims: dict) -> User:
    role = role_from_claims(claims)
    issuer, subject = get_settings().oidc_issuer.rstrip("/"), claims["sub"]
    username = claims.get("preferred_username")
    if not isinstance(subject, str) or not subject or len(subject) > 255:
        raise HTTPException(401, "统一账号标识无效")
    if not isinstance(username, str) or not username.strip() or len(username) > 100:
        raise HTTPException(403, "统一账号用户名无效，请联系管理员")
    email = claims.get("email")
    if not isinstance(email, str) or len(email) > 200:
        email = None
    async with short_session() as session:
        identity = (await session.execute(select(UserIdentity).where(
            UserIdentity.issuer == issuer, UserIdentity.subject == subject))).scalar_one_or_none()
        if identity:
            user = await session.get(User, identity.user_id)
            # A local disable remains an emergency block; SSO must never re-enable it.
            if not user or not user.is_active:
                raise HTTPException(401, "用户已停用，请联系管理员")
        else:
            collision = (await session.execute(select(User.id).where(User.username == username))).scalar_one_or_none()
            if collision:
                # Another app may have committed this exact identity since our first lookup.
                identity = (await session.execute(select(UserIdentity).where(
                    UserIdentity.issuer == issuer, UserIdentity.subject == subject))).scalar_one_or_none()
                if not identity or identity.user_id != collision:
                    raise HTTPException(409, "该用户名已有本地账号，请管理员完成身份绑定后再登录")
                user = await session.get(User, identity.user_id)
                if not user or not user.is_active:
                    raise HTTPException(401, "用户已停用，请联系管理员")
            else:
                user = User(username=username, email=email, role=role, is_active=True,
                            password_hash=hash_password(secrets.token_urlsafe(32)),
                            must_change_password=False)
                session.add(user)
                try:
                    await session.flush()
                    session.add(UserIdentity(issuer=issuer, subject=subject, user_id=user.id))
                    await session.flush()
                except IntegrityError as exc:
                    await session.rollback()
                    # Recover only the exact committed issuer/subject binding, never the name.
                    identity = (await session.execute(select(UserIdentity).where(
                        UserIdentity.issuer == issuer, UserIdentity.subject == subject))).scalar_one_or_none()
                    user = await session.get(User, identity.user_id) if identity else None
                    if not user:
                        raise HTTPException(409, "用户名已存在，请管理员完成身份绑定后再登录") from exc
                    if not user.is_active:
                        raise HTTPException(401, "用户已停用，请联系管理员")
        # Role comes exclusively from signed client-role claims, never a browser or local override.
        # Do not persist it on existing users: out-of-order valid tokens could overwrite newer roles.
        # Flush profile changes first, then return an unattached per-request principal.
        user.email = email
        user.must_change_password = False
        await session.commit()
        await session.refresh(user)
        session.expunge(user)
        user.role = role
        return user


async def authenticate(token: str) -> User:
    if enabled():
        return await resolve_oidc_user(await verify_access_token(token))
    try:
        claims = decode_jwt(token)
        async with short_session() as session:
            user = await session.get(User, claims["sub"])
        if not user or not user.is_active:
            raise ValueError("Inactive user")
        return user
    except Exception as exc:
        raise HTTPException(401, "无效凭证") from exc
