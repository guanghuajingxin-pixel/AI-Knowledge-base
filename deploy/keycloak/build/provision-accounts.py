#!/usr/bin/env python3
"""把业务库账号搬进 Keycloak，并生成切换时用的显式身份映射表。

设计约束（与 deploy/keycloak/README.md 一致）：
- 只建账号、只授 kb-api 客户端角色，绝不按用户名/邮箱自动合并业务数据。
- Keycloak 不接受 create users 指定 UUID，因此把 local_id <-> kc_id 写进映射表，
  切换时由 scripts/keycloak_accounts.py --user-id/--subject 逐条精确绑定。
- 密码只设临时密码 + UPDATE_PASSWORD，首次登录强制改密；不复制本地密码哈希。
- 幂等：已存在的用户不重置密码、不扩权，只补齐缺失的客户端角色。

在身份主机上执行：
  python3 provision-accounts.py --dry-run    # 只看计划
  python3 provision-accounts.py              # 建号并授权
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import string
import subprocess
import sys

CONTAINER = os.environ.get('CONTAINER', 'jack-identity-keycloak-1')
PG_CONTAINER = os.environ.get('PG_CONTAINER', 'kge-postgres')
PG_USER = os.environ.get('PG_USER', 'dev')
PG_DB = os.environ.get('PG_DB', 'dev_db')
REALM = os.environ.get('REALM', 'jack')
AUDIENCE = os.environ.get('AUDIENCE', 'kb-api')
KEYCLOAK = os.environ.get('KEYCLOAK', 'http://localhost:8080')
ENV_FILE = Path(os.environ.get('ENV_FILE', '/opt/kge/deploy/keycloak/.env'))
MAP_FILE = Path(os.environ.get('MAP_FILE', '/opt/kge/deploy/keycloak/account-map.json'))
PW_FILE = Path(os.environ.get('PW_FILE', '/opt/kge/deploy/keycloak/initial-passwords.env'))


def env_value(name: str) -> str:
    """从 .env 取 bootstrap 管理员凭据，不回显。"""
    for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
        if line.startswith(name + '='):
            return line.split('=', 1)[1].strip()
    return ''


def kcadm(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(['docker', 'exec', '-i', CONTAINER, '/opt/keycloak/bin/kcadm.sh',
                           *args, '-r', REALM, '--server', KEYCLOAK],
                          capture_output=True, text=True)


def kcadm_json(*args: str):
    result = kcadm(*args)
    if result.returncode != 0:
        raise RuntimeError(result.stdout.strip() or result.stderr.strip())
    body = result.stdout.strip()
    return json.loads(body) if body else None


def login() -> None:
    user, password = env_value('KEYCLOAK_ADMIN'), env_value('KEYCLOAK_ADMIN_PASSWORD')
    if not user or not password:
        raise SystemExit(f'缺少 bootstrap 管理员凭据：{ENV_FILE}')
    result = subprocess.run(['docker', 'exec', '-i', CONTAINER, '/opt/keycloak/bin/kcadm.sh',
                             'config', 'credentials', '--server', KEYCLOAK, '--realm', 'master',
                             '--user', user, '--password', password],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit('无法登录身份中心：' + (result.stdout.strip() or result.stderr.strip()))


def temp_password() -> str:
    alphabet = (string.ascii_uppercase * 2 + string.ascii_lowercase * 3
                + string.digits * 2 + '!@#$%*')
    while True:
        value = ''.join(secrets.choice(alphabet) for _ in range(14))
        if (any(c.isupper() for c in value) and any(c.islower() for c in value)
                and any(c.isdigit() for c in value)):
            return value


def local_users() -> list[dict]:
    # 钉钉自动建的账号在 dingtalk_bindings 里有真实姓名，拿来填 Keycloak 的
    # firstName/lastName：Keycloak 判定「账号是否配置完整」要求姓名非空，
    # 缺姓名会让统一登录直接报 Account is not fully set up。
    query = ("select u.id, u.username, coalesce(u.email,''), u.role, u.is_active, "
             "coalesce(b.dt_name,'') from users u "
             "left join dingtalk_bindings b on b.user_id = u.id order by u.created_at")
    result = subprocess.run(['docker', 'exec', PG_CONTAINER, 'psql', '-U', PG_USER, '-d', PG_DB,
                             '-A', '-t', '-F', '\t', '-c', query], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or '读取业务用户失败')
    rows = []
    for line in result.stdout.splitlines():
        parts = line.rstrip('\n').split('\t')
        if len(parts) != 6:
            continue
        display = parts[5].strip() or DEFAULT_NAMES.get(parts[1], '')
        rows.append({'local_id': parts[0], 'username': parts[1], 'email': parts[2],
                     'role': parts[3], 'active': parts[4] == 't',
                     'last_name': display[:1] or '-', 'first_name': display[1:] or display})
    if not rows:
        raise SystemExit('业务库里没有账号')
    return rows


DEFAULT_NAMES = {'admin': '平台管理员'}


def keycloak_users() -> dict[str, dict]:
    rows = kcadm_json('get', 'users', '--fields', 'id,username,requiredActions') or []
    return {row['username']: row for row in rows}


_CLIENTS: dict[str, str] = {}


def client_uuid(client_id: str) -> str:
    if client_id not in _CLIENTS:
        rows = kcadm_json('get', 'clients', '--fields', 'id,clientId') or []
        _CLIENTS.update({row.get('clientId'): row.get('id', '') for row in rows})
    return _CLIENTS.get(client_id, '')


def role_names(user_id: str) -> set[str]:
    """该用户已持有的 kb-api 客户端角色名（应用侧权限唯一来源）。"""
    resource = client_uuid(AUDIENCE)
    if not resource:
        return set()
    rows = kcadm_json('get', f'users/{user_id}/role-mappings/clients/{resource}') or []
    return {row.get('name') for row in rows if isinstance(row, dict)}


def load_map() -> dict:
    if MAP_FILE.exists():
        return json.loads(MAP_FILE.read_text(encoding='utf-8'))
    return {}


def save_map(mapping: dict) -> None:
    MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    mapping['note'] = '切换时按此表逐条执行 keycloak_accounts.py --user-id/--subject --apply'
    MAP_FILE.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding='utf-8')
    MAP_FILE.chmod(0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='只打印计划，不创建任何对象')
    args = parser.parse_args()

    users, mapping = local_users(), load_map()
    if args.dry_run:
        for user in users:
            print(f"{'建号' if user['active'] else '跳过'}\t{user['username']}"
                  f"\tlocal={user['local_id']}\t角色={user['role']}")
        return 0

    login()
    existing = keycloak_users()
    created, updated, skipped, failed = [], [], [], []
    passwords: list[str] = []

    for user in users:
        name, role, local_id = user['username'], user['role'], user['local_id']
        if not user['active']:
            skipped.append(f'{name}（业务库已停用，不在身份中心开放登录）')
            continue
        remote = existing.get(name)
        password_needed = False
        try:
            if remote is None:
                body = {'username': name, 'enabled': True, 'emailVerified': False,
                        'firstName': user['first_name'], 'lastName': user['last_name'],
                        'requiredActions': ['UPDATE_PASSWORD']}
                if user['email']:
                    body['email'] = user['email']
                new_id = (kcadm_json('create', 'users', '-b', json.dumps(body, ensure_ascii=False),
                                     '-i') or '').strip()
                if not new_id:
                    failed.append(f'{name}: 创建失败')
                    continue
                remote = {'id': new_id, 'username': name}
                created.append(name)
                password_needed = True
            else:
                # 早先建的账号可能没填姓名（会被统一登录判为未配置完整），补齐
                kcadm('update', f'users/{remote["id"]}', '-s', f'firstName={user["first_name"]}',
                      '-s', f'lastName={user["last_name"]}')
                created_or_not = mapping.get(name, {}).get('password_issued')
                password_needed = not created_or_not
                updated.append(name)

            # 角色只按业务库当前值补齐，绝不做权限提升
            add = kcadm('add-roles', '--uid', remote['id'], '--cclientid', AUDIENCE,
                        '--rolename', role)
            granted = role_names(remote['id'])
            if role not in granted:
                failed.append(f'{name}: 授予 {AUDIENCE}/{role} 未生效'
                              f'（当前 {sorted(granted) or "无"}）- {add.stdout.strip()}')
                continue

            if password_needed:
                password = temp_password()
                # KC 26 的 create users/<id>/reset-password 走不通，用官方 set-password，
                # -t 让密码一次性，首次登录必须改。
                result = kcadm('set-password', '--userid', remote['id'], '-t', '-p', password)
                if result.returncode != 0:
                    failed.append(f'{name}: 临时密码设置失败 - '
                                  f'{result.stdout.strip() or result.stderr.strip()}')
                    continue
                passwords.append(f'KC_PASSWORD_{name}={password}')
                mapping[name] = {'local_id': local_id, 'kc_id': remote['id'], 'role': role,
                                 'password_issued': True}
            else:
                mapping.setdefault(name, {'local_id': local_id, 'kc_id': remote['id'],
                                          'role': role, 'password_issued': False})
        except RuntimeError as exc:
            failed.append(f'{name}: {exc}')

    save_map(mapping)
    if passwords:
        existing_text = PW_FILE.read_text(encoding='utf-8') if PW_FILE.exists() else ''
        header = '' if existing_text else '# 一次性临时密码，首次登录必须改密。\n'
        PW_FILE.write_text(existing_text + header + '\n'.join(passwords) + '\n', encoding='utf-8')
        PW_FILE.chmod(0o600)

    print(f'新建 {len(created)} / 已存在 {len(updated)} / 跳过 {len(skipped)} / 失败 {len(failed)}')
    for note in skipped:
        print('  - ' + note)
    for note in failed:
        print('  ! ' + note)
    print(f'映射表：{MAP_FILE}')
    if passwords:
        print(f'临时密码：{PW_FILE}（600 权限，未打印到终端）')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
