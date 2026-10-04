from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from kb_common.database import get_session
from kb_common.models import DingtalkBinding, Setting, User
from kb_common.security import verify_password, hash_password
from kb_common.oidc import enabled, oidc_config, require_local_auth
from pydantic import BaseModel, field_validator

from app.deps import get_current_user
from app.services.audit import write_audit

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _check_strength(p: str) -> None:
    if len(p) < 8 or len(p) > 64:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "密码长度须为 8-64 位")
    if not any(c.isalpha() for c in p) or not any(c.isdigit() for c in p):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "密码须同时包含字母和数字")


async def _binding_of(s: AsyncSession, user_id) -> DingtalkBinding | None:
    return (await s.execute(
        select(DingtalkBinding).where(DingtalkBinding.user_id == user_id)
    )).scalar_one_or_none()


class LoginIn(BaseModel):
    username: str
    password: str

@router.post("/login")
async def login(body: LoginIn, s: AsyncSession = Depends(get_session)):
    require_local_auth()
    u = (await s.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    if not u or not verify_password(body.password, u.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    if not u.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户已停用，请联系管理员")
    from app.services.dingtalk_identity import issue_token
    binding = await _binding_of(s, u.id)
    return issue_token(u, binding)


class SetPasswordIn(BaseModel):
    new_password: str
    new_password_confirm: str

    @field_validator("new_password_confirm")
    @classmethod
    def _match(cls, v, info):
        if "new_password" in info.data and v != info.data["new_password"]:
            raise ValueError("两次输入的密码不一致")
        return v


class ChangePasswordIn(SetPasswordIn):
    old_password: str


@router.post("/set-password")
async def set_password(body: SetPasswordIn, request: Request,
                       u: User = Depends(get_current_user),
                       s: AsyncSession = Depends(get_session)):
    """首次设密：钉钉建档 / 管理员重置后 must_change_password=true 的用户必经此接口。"""
    require_local_auth()
    tu = await s.get(User, u.id)
    if not tu:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    if not tu.must_change_password and tu.password_updated_at is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "当前账号无需设密，请使用「修改密码」功能")
    _check_strength(body.new_password)
    tu.password_hash = hash_password(body.new_password)
    tu.must_change_password = False
    tu.password_updated_at = datetime.now()
    await s.commit()
    await s.refresh(tu)
    await write_audit(s, actor_id=tu.id, action="self_set_password", target_type="user",
                      target_id=str(tu.id), request=request)
    from app.services.dingtalk_identity import issue_token
    binding = await _binding_of(s, tu.id)
    out = issue_token(tu, binding)   # 新 token 不再带 mcp 标记，前端替换本地 token
    out["ok"] = True
    return out


@router.post("/change-password")
async def change_password(body: ChangePasswordIn, request: Request,
                          u: User = Depends(get_current_user),
                          s: AsyncSession = Depends(get_session)):
    require_local_auth()
    tu = await s.get(User, u.id)
    if not tu:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    if not verify_password(body.old_password, tu.password_hash):
        await write_audit(s, actor_id=tu.id, action="self_change_password_failed",
                          target_type="user", target_id=str(tu.id),
                          detail={"reason": "old_password_mismatch"}, request=request)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "旧密码不正确")
    _check_strength(body.new_password)
    if verify_password(body.new_password, tu.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "新密码不能与旧密码相同")
    tu.password_hash = hash_password(body.new_password)
    tu.must_change_password = False
    tu.password_updated_at = datetime.now()
    await s.commit()
    await write_audit(s, actor_id=tu.id, action="self_change_password", target_type="user",
                      target_id=str(tu.id), request=request)
    return {"ok": True}


class DingtalkLoginIn(BaseModel):
    auth_code: str
    channel: str = "h5"   # h5=工作台免登 | qr=PC 扫码（统一登录码）


@router.get("/dingtalk-config")
async def dingtalk_config(request: Request, redirect_uri: str = "",
                          s: AsyncSession = Depends(get_session)):
    """公开端点：H5 免登 / PC 扫码前置（corpId + appKey + 开关）。未配置 corpId 时前端回退账号登录。"""
    if enabled():
        return {"corp_id": "", "app_key": "", "auto_login_enabled": False, "qr_login_enabled": False, "redirect_uri": ""}
    rows = (await s.execute(select(Setting).where(Setting.key.in_(
        ["dingtalk_corp_id", "dingtalk_app_key", "dingtalk_qr_login_enabled"])))).scalars().all()
    cfg = {r.key: (r.value or "").strip() for r in rows}
    corp = cfg.get("dingtalk_corp_id", "")
    qr_raw = cfg.get("dingtalk_qr_login_enabled", "")
    qr_enabled = True if qr_raw == "" else qr_raw.lower() in ("1", "true", "yes", "on")
    # 回调地址：优先请求显式传入（须与 Origin 同域防开放重定向），否则用 Origin 推导
    origin = (request.headers.get("origin") or "").rstrip("/")
    cb = redirect_uri.strip()
    if cb and origin and not cb.startswith(origin + "/"):
        cb = ""
    if not cb:
        cb = f"{origin}/login/callback" if origin else ""
    return {"corp_id": corp, "app_key": cfg.get("dingtalk_app_key", ""),
            "auto_login_enabled": bool(corp), "qr_login_enabled": bool(qr_enabled and corp),
            "redirect_uri": cb}


@router.post("/dingtalk-login")
async def dingtalk_login(body: DingtalkLoginIn, s: AsyncSession = Depends(get_session)):
    """钉钉认证登录：H5 免登 / PC 扫码 → 员工身份 → 本地用户（首次自动建档+强制设密）→ JWT。"""
    require_local_auth()
    from kb_common.clients import dingtalk_client
    from app.services.dingtalk_identity import issue_token, resolve_user

    row = (await s.execute(select(Setting).where(Setting.key == "dingtalk_corp_id"))).scalar_one_or_none()
    corp_id = ((row.value if row else "") or "").strip()
    if not corp_id:
        raise HTTPException(400, "钉钉免登未启用：请先在系统配置中填写 corpId")
    # 配置页保存的凭证即时生效（不依赖重启加载 env）
    await dingtalk_client.sync_runtime_config()
    try:
        if body.channel == "qr":
            info = await dingtalk_client.get_user_info_by_qr_code(body.auth_code.strip())
        else:
            info = await dingtalk_client.get_user_info_by_code(body.auth_code.strip())
    except RuntimeError as e:
        raise HTTPException(401, str(e))
    try:
        user, binding = await resolve_user(s, corp_id, info["userid"],
                                           info.get("unionid", ""), info.get("name", ""))
    except RuntimeError as e:
        raise HTTPException(403, str(e))
    out = issue_token(user, binding)
    out["user"]["display_name"] = info.get("name") or user.username
    return out


class BindDingtalkIn(BaseModel):
    auth_code: str
    channel: str = "qr"


@router.post("/users/me/dingtalk-binding")
async def bind_my_dingtalk(body: BindDingtalkIn, request: Request,
                           u: User = Depends(get_current_user),
                           s: AsyncSession = Depends(get_session)):
    """已登录账号绑定钉钉身份：老用户（密码账号）接入按用户维度的钉钉权限。"""
    require_local_auth()
    from kb_common.clients import dingtalk_client
    from app.services import dingtalk_operator
    from app.services.dingtalk_identity import issue_token

    row = (await s.execute(select(Setting).where(Setting.key == "dingtalk_corp_id"))).scalar_one_or_none()
    corp_id = ((row.value if row else "") or "").strip()
    if not corp_id:
        raise HTTPException(400, "钉钉未启用：请先在系统配置中填写 corpId")
    if await _binding_of(s, u.id):
        raise HTTPException(409, "当前账号已绑定钉钉，如需换绑请先由管理员解绑")
    await dingtalk_client.sync_runtime_config()
    try:
        info = (await dingtalk_client.get_user_info_by_qr_code(body.auth_code.strip())
                if body.channel == "qr"
                else await dingtalk_client.get_user_info_by_code(body.auth_code.strip()))
    except RuntimeError as e:
        raise HTTPException(401, str(e))
    taken = (await s.execute(select(DingtalkBinding).where(
        DingtalkBinding.corp_id == corp_id,
        DingtalkBinding.dt_userid == info["userid"]))).scalar_one_or_none()
    if taken:
        other = await s.get(User, taken.user_id)
        raise HTTPException(409, f"该钉钉身份已绑定账号 {other.username if other else taken.user_id}，"
                              f"请先由管理员解绑")
    binding = DingtalkBinding(user_id=u.id, corp_id=corp_id, dt_userid=info["userid"],
                              dt_unionid=info.get("unionid") or None,
                              dt_name=info.get("name") or None)
    s.add(binding)
    await s.commit()
    await s.refresh(binding)
    dingtalk_operator.invalidate_user(u.id)
    await write_audit(s, actor_id=u.id, action="self_bind_dingtalk", target_type="user",
                      target_id=str(u.id), detail={"dt_userid": info["userid"]}, request=request)
    out = issue_token(u, binding)
    out["ok"] = True
    return out


@router.get("/config")
async def authentication_config():
    """Public metadata only; client IDs are public, no secrets are exposed."""
    return oidc_config()
