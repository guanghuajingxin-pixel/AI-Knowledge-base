from fastapi import APIRouter, Depends, HTTPException, Request, status
from kb_common.models import User, ApiKey, DingtalkBinding
from kb_common.security import hash_password
from kb_common.oidc import require_local_auth, enabled, oidc_config
from kb_common.config import get_settings
import secrets, hashlib, uuid
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from kb_common.database import get_session
from app.deps import get_current_user, require_role
from app.services import dingtalk_operator
from app.services.audit import write_audit
from app.services.dingtalk_identity import user_public_dict

router = APIRouter(prefix="/api/v1", tags=["users"])


def _user_dict(u: User) -> dict:
    return {"id": str(u.id), "username": u.username, "email": u.email or "",
            "role": u.role, "is_active": u.is_active,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "must_change_password": bool(u.must_change_password),
            "password_updated_at": (u.password_updated_at.isoformat()
                                    if u.password_updated_at else None)}


def _binding_dict(b: DingtalkBinding) -> dict:
    return {"dt_name": b.dt_name or "", "dt_userid": b.dt_userid or "",
            "dt_unionid": b.dt_unionid or "", "corp_id": b.corp_id or "",
            "bound_at": b.created_at.isoformat() if b.created_at else None}


async def _bindings_map(s: AsyncSession, user_ids: list) -> dict:
    if not user_ids:
        return {}
    rows = (await s.execute(select(DingtalkBinding).where(
        DingtalkBinding.user_id.in_(user_ids)))).scalars().all()
    return {r.user_id: r for r in rows}


@router.get("/users/me")
async def me(u: User = Depends(get_current_user), s: AsyncSession = Depends(get_session)):
    """Return full UserInfo - aligns with frontend type {id,username,email,role,is_active,created_at}."""
    binding = (await s.execute(select(DingtalkBinding).where(
        DingtalkBinding.user_id == u.id))).scalar_one_or_none()
    return user_public_dict(u, binding)


class UserCreate(BaseModel):
    username: str
    password: str
    email: str | None = None
    role: str = "viewer"   # 校验在 handler


class UserUpdate(BaseModel):
    email: str | None = None
    role: str | None = None
    is_active: bool | None = None
    password: str | None = None   # 非空则重置
    must_change_password: bool | None = None  # 重置密码时默认置 true（强制下次改密）


VALID_ROLES = {"super_admin", "admin", "editor", "viewer"}


@router.get("/users")
async def list_users(u: User = Depends(require_role("super_admin", "admin")),
                     s: AsyncSession = Depends(get_session), page: int = 1, size: int = 20,
                     include_binding: bool = False):
    page = max(1, page); size = max(1, min(100, size))
    total = (await s.execute(select(func.count(User.id)))).scalar_one()
    rows = (await s.execute(select(User).order_by(User.created_at.desc())
                            .offset((page - 1) * size).limit(size))).scalars().all()
    items = [_user_dict(r) for r in rows]
    if include_binding:
        bmap = await _bindings_map(s, [r.id for r in rows])
        for it, r in zip(items, rows):
            b = bmap.get(r.id)
            it["dingtalk_binding"] = _binding_dict(b) if b else None
    return {"items": items, "total": total, "page": page, "size": size}


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(body: UserCreate, request: Request,
                      u: User = Depends(require_role("super_admin", "admin")),
                      s: AsyncSession = Depends(get_session)):
    require_local_auth()
    if body.role not in VALID_ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"非法角色: {body.role}")
    if (await s.execute(select(User).where(User.username == body.username))).scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, "用户名已存在")
    nu = User(username=body.username, password_hash=hash_password(body.password),
              email=body.email, role=body.role,
              must_change_password=True,   # 管理员代建账号同样强制首登改密
              password_updated_at=None)
    s.add(nu); await s.commit(); await s.refresh(nu)
    await write_audit(s, actor_id=u.id, action="admin_create_user", target_type="user",
                      target_id=str(nu.id), detail={"role": nu.role}, request=request)
    return _user_dict(nu)


@router.put("/users/{user_id}")
async def update_user(user_id: uuid.UUID, body: UserUpdate, request: Request,
                      u: User = Depends(require_role("super_admin", "admin")),
                      s: AsyncSession = Depends(get_session)):
    require_local_auth()
    tu = await s.get(User, user_id)
    if not tu:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    # 不允许把最后一个 super_admin 降级或禁用
    if tu.role == "super_admin" and ((body.role is not None and body.role != "super_admin")
                                     or (body.is_active is False)):
        cnt = (await s.execute(select(func.count(User.id))
                .where(User.role == "super_admin", User.is_active == True))).scalar_one()
        if cnt <= 1:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "至少保留一个启用的超级管理员")
    audit_detail: dict = {}
    if body.email is not None: tu.email = body.email
    if body.role is not None:
        if body.role not in VALID_ROLES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"非法角色: {body.role}")
        if body.role != tu.role:
            audit_detail["role"] = {"from": tu.role, "to": body.role}
        tu.role = body.role
    if body.is_active is not None: tu.is_active = body.is_active
    if body.password:
        if len(body.password) < 8:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "密码长度至少 8 位")
        tu.password_hash = hash_password(body.password)
        # 重置密码默认强制下次改密；body 显式 false 才豁免
        tu.must_change_password = body.must_change_password is not False
        tu.password_updated_at = None
        audit_detail["reset_password"] = True
        audit_detail["must_change_password"] = tu.must_change_password
    elif body.must_change_password is not None:
        tu.must_change_password = body.must_change_password
        audit_detail["must_change_password"] = body.must_change_password
    await s.commit(); await s.refresh(tu)
    if audit_detail:
        await write_audit(s, actor_id=u.id,
                          action="admin_reset_password" if audit_detail.get("reset_password")
                          else "admin_update_user",
                          target_type="user", target_id=str(tu.id),
                          detail=audit_detail, request=request)
    return _user_dict(tu)


@router.delete("/users/{user_id}")
async def delete_user(user_id: uuid.UUID, request: Request,
                      u: User = Depends(require_role("super_admin", "admin")),
                      s: AsyncSession = Depends(get_session)):
    require_local_auth()
    if user_id == u.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能删除自己")
    tu = await s.get(User, user_id)
    if not tu:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    if tu.role == "super_admin":
        cnt = (await s.execute(select(func.count(User.id))
                .where(User.role == "super_admin", User.is_active == True))).scalar_one()
        if cnt <= 1:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "至少保留一个超级管理员")
    await s.delete(tu); await s.commit()
    dingtalk_operator.invalidate_user(user_id)
    await write_audit(s, actor_id=u.id, action="admin_delete_user", target_type="user",
                      target_id=str(user_id), request=request)
    return {"ok": True}


@router.delete("/users/{user_id}/dingtalk-binding")
async def unbind_dingtalk(user_id: uuid.UUID, request: Request,
                          u: User = Depends(require_role("super_admin", "admin")),
                          s: AsyncSession = Depends(get_session)):
    """解绑钉钉（换岗/离职）：保留本地账号与密码登录能力，清除钉钉权限映射。"""
    require_local_auth()
    tu = await s.get(User, user_id)
    if not tu:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    if tu.role == "super_admin":
        cnt = (await s.execute(select(func.count(User.id))
                .where(User.role == "super_admin", User.is_active == True))).scalar_one()
        if cnt <= 1:
            raise HTTPException(status.HTTP_409_CONFLICT, "最后一名超级管理员不可解绑钉钉")
    b = (await s.execute(select(DingtalkBinding).where(
        DingtalkBinding.user_id == user_id))).scalar_one_or_none()
    if not b:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "该用户未绑定钉钉")
    detail = {"dt_userid": b.dt_userid, "dt_name": b.dt_name or ""}
    await s.delete(b)
    await s.commit()
    dingtalk_operator.invalidate_user(user_id)
    dingtalk_operator.invalidate_source()   # owner 绑定变化影响同步身份解析
    await write_audit(s, actor_id=u.id, action="admin_unbind_dingtalk", target_type="user",
                      target_id=str(user_id), detail=detail, request=request)
    return {"ok": True}

class ApiKeyIn(BaseModel):
    name: str

@router.post("/auth/api-keys")
async def create_key(body: ApiKeyIn, u: User = Depends(get_current_user),
                     s: AsyncSession = Depends(get_session)):
    require_local_auth()
    raw = "kb_" + secrets.token_hex(24)
    k = ApiKey(user_id=u.id, name=body.name, key_hash=hashlib.sha256(raw.encode()).hexdigest(),
               key_prefix=raw[:10])
    s.add(k); await s.commit(); await s.refresh(k)
    # raw_key 仅此一次返回 - aligns with frontend ApiKeyCreated.raw_key
    return {"id": str(k.id), "name": k.name, "key_prefix": k.key_prefix,
            "raw_key": raw, "is_active": k.is_active,
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
            "created_at": k.created_at.isoformat() if k.created_at else None}

@router.get("/auth/api-keys")
async def list_keys(u: User = Depends(get_current_user), s: AsyncSession = Depends(get_session)):
    from sqlalchemy import select
    rows = (await s.execute(select(ApiKey).where(ApiKey.user_id == u.id)
                            .order_by(ApiKey.created_at.desc()))).scalars().all()
    # key_prefix (not prefix) - aligns with frontend ApiKey type
    return [{"id": str(r.id), "name": r.name, "key_prefix": r.key_prefix,
             "is_active": r.is_active,
             "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
             "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]

@router.delete("/auth/api-keys/{key_id}")
async def delete_key(key_id: uuid.UUID, u: User = Depends(get_current_user),
                     s: AsyncSession = Depends(get_session)):
    k = await s.get(ApiKey, key_id)
    if not k:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Key 不存在")
    if k.user_id != u.id and u.role not in ("super_admin", "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权删除他人 Key")
    await s.delete(k); await s.commit()
    return {"ok": True}


@router.get("/auth/account-management")
async def account_management(u: User = Depends(get_current_user)):
    cfg = oidc_config()
    if not enabled():
        return {"provider": "local", "account_url": "/settings/account",
                "admin_url": "/admin/users" if u.role in ("super_admin", "admin") else None}
    return {"provider": "keycloak", "account_url": cfg["account_url"],
            "admin_url": cfg["admin_url"] if u.role in ("super_admin", "admin") else None}
