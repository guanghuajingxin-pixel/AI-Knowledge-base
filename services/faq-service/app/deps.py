from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from kb_common.database import get_session
from kb_common.oidc import authenticate
from kb_common.models import User

bearer = HTTPBearer()

async def get_current_user(cred: HTTPAuthorizationCredentials = Depends(bearer)) -> User:
    return await authenticate(cred.credentials)


def require_role(*roles):
    """角色校验闭包：依赖 get_current_user，u.role 不在 roles 中则 403。"""
    async def checker(u: User = Depends(get_current_user)) -> User:
        if u.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "权限不足")
        return u
    return checker
