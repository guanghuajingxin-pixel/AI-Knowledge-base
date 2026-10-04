"""Export local users or explicitly bind imported Keycloak subjects.

cd services/kb-common && uv run python ../../scripts/keycloak_accounts.py --help
No password hashes are exported. Bindings default to a read-only plan.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import uuid
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "kb-common"))

import httpx
from sqlalchemy import select
from kb_common.config import get_settings
from kb_common.database import short_session
from kb_common.models import User, UserIdentity


async def main(args):
    issuer = (args.issuer or get_settings().oidc_issuer).rstrip('/')
    async with short_session() as session:
        users = list((await session.execute(select(User).order_by(User.username))).scalars().all())
        if args.export:
            path = Path(args.export)
            # Keycloak partial import; credentials must be reset in the identity center.
            data = {'ifResourceExists': 'FAIL', 'users': [
                {'id': str(user.id), 'username': user.username, 'enabled': user.is_active,
                 'email': user.email or '', 'emailVerified': False,
                 'requiredActions': ['UPDATE_PASSWORD'],
                 'clientRoles': {get_settings().oidc_audience: [user.role]}}
                for user in users
            ]}
            with path.open('x') as output:
                os.chmod(path, 0o600)
                json.dump(data, output, ensure_ascii=False, indent=2)
            print(f'已导出 {len(users)} 个账号（不含密码/哈希），文件：{path}')
            return
        if not issuer or '/realms/' not in issuer:
            raise SystemExit('请提供 --issuer 或 OIDC_ISSUER')
        base, realm = issuer.rsplit('/realms/', 1)
        admin_token = os.environ.get('OIDC_ADMIN_TOKEN', '')
        if not admin_token:
            raise SystemExit('请通过 OIDC_ADMIN_TOKEN 提供短期管理员访问令牌；不要写入源码')
        selected = users if args.bind_imported else [user for user in users if str(user.id) == args.user_id]
        if not selected:
            raise SystemExit('未找到需要绑定的本地用户')
        async with httpx.AsyncClient(timeout=15, headers={'Authorization': f'Bearer {admin_token}'}) as client:
            for user in selected:
                subject = args.subject if args.user_id else str(user.id)
                uuid.UUID(subject)  # Reject paths/queries; Keycloak managed user subjects are UUIDs.
                response = await client.get(f'{base}/admin/realms/{realm}/users/{subject}')
                response.raise_for_status()
                remote = response.json()
                if remote.get('id') != subject or remote.get('username') != user.username:
                    raise SystemExit(f'账号 {user.username} 的远端标识或用户名不匹配，停止绑定')
                existing = (await session.execute(select(UserIdentity).where(
                    UserIdentity.issuer == issuer, UserIdentity.subject == subject))).scalar_one_or_none()
                own = (await session.execute(select(UserIdentity).where(
                    UserIdentity.issuer == issuer, UserIdentity.user_id == user.id))).scalar_one_or_none()
                if (existing and existing.user_id != user.id) or (own and own.subject != subject):
                    raise SystemExit(f'账号 {user.username} 存在冲突绑定，停止操作')
                print(f'{"已绑定" if existing else "待绑定"}: {user.username} ({user.id}) -> {subject}')
                if args.apply and not existing:
                    session.add(UserIdentity(user_id=user.id, issuer=issuer, subject=subject))
        if args.apply:
            await session.commit()
            print('身份绑定已提交，原业务用户 ID、知识归属及钉钉绑定保持不变。')
        else:
            print('只读预览；检查无误后添加 --apply 执行。')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--export', metavar='FILE', help='导出供 Keycloak 部分导入的账号清单')
    modes.add_argument('--bind-imported', action='store_true', help='绑定已按原 UUID 导入的账号')
    modes.add_argument('--user-id', help='明确指定本地用户 UUID')
    parser.add_argument('--subject', help='与 --user-id 配合，指定 Keycloak 用户 UUID')
    parser.add_argument('--issuer', help='例如 https://sso.example.com/realms/jack')
    parser.add_argument('--apply', action='store_true', help='将已验证的映射写入数据库')
    arguments = parser.parse_args()
    if arguments.user_id and not arguments.subject:
        parser.error('--user-id 必须同时提供 --subject')
    asyncio.run(main(arguments))
