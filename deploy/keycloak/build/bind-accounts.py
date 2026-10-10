#!/usr/bin/env python3
"""按 account-map.json 批量完成「业务账号 <-> 统一身份」绑定（切换日第 3 步）。

复用官方迁移脚本 scripts/keycloak_accounts.py，不另写一套校验/写库逻辑：
  - 管理令牌用服务账号（platform-key-manager，client_credentials）在应用容器内换取，
    访问的就是应用将来访问身份中心的同一条路径， issuer 与应用配置逐字一致。
  - 官方脚本会向身份中心核对远端 UUID 与用户名，不一致直接停；已绑定的幂等跳过。
  - 绑定只新增 (issuer, subject) -> user_id 映射，业务 User ID、知识归属、
    钉钉绑定全部不动。

在身份主机上执行（证书与 KEYCLOAK_HOSTNAME 定稿之后）：
  python3 bind-accounts.py --issuer https://sso.<域名>/realms/jack          # 只读预览
  python3 bind-accounts.py --issuer https://sso.<域名>/realms/jack --apply   # 实际写入
"""
import argparse
import json
import shlex
import subprocess
import sys

CONTAINER = 'kge-kb-api'
RUNTIME = {
    'kge-kb-api': '/app/services/kb-api/.venv/bin/python',
    'kge-kb-api-verify': '/app/services/kb-api/.venv/bin/python',
    'kge-faq-service': '/app/services/faq-service/.venv/bin/python',
}
KCADM = '/opt/keycloak/bin/kcadm.sh'
KCONTAINER = 'jack-identity-keycloak-1'
MAP_FILE = 'account-map.json'  # 与本脚本同目录，或 MAP_FILE 环境变量覆盖
KEYS_FILE = '/opt/kge/deploy/intranet/platform-keys.env'
HERE = __import__('pathlib').Path(__file__).resolve().parent


def docker_exec(container, argv, stdin=None, env=None):
    args = ['docker', 'exec', '-i']
    for key, value in (env or {}).items():
        args += ['-e', f'{key}={value}']
    return subprocess.run(args + [container, *argv], input=stdin,
                          capture_output=True, text=True)


def read_env(path):
    values = {}
    for line in open(path, encoding='utf-8'):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip()
    return values


def admin_token(container, issuer, client_id, client_secret, runtime):
    """凭据经 docker API 注入容器环境，不进容器内命令行。"""
    # 应用镜像里没有 curl，直接用运行时 python + httpx
    fetch = (
        'import httpx, os, sys'
        '\ntry:'
        '\n    r = httpx.post(os.environ["ISSUER"] + "/protocol/openid-connect/token", data='
        '{"grant_type": "client_credentials", "client_id": os.environ["KC_ID"],'
        ' "client_secret": os.environ["KC_SECRET"]}, timeout=15)'
        '\n    print(r.json().get("access_token", "") if r.status_code == 200 else "")'
        '\nexcept Exception as exc:'
        '\n    print(type(exc).__name__, file=sys.stderr)')
    script = f'{shlex.quote(runtime)} -c {shlex.quote(fetch)}'
    result = docker_exec(container, ['sh', '-c', script], env={
        'ISSUER': issuer, 'KC_ID': client_id, 'KC_SECRET': client_secret})
    token = (result.stdout or '').strip()
    if not token:
        sys.exit('换取管理令牌失败：容器内访问 '
                 + issuer + ' 异常 ' + (result.stderr or result.stdout or '').strip()[:200])
    return token


def remote_username(subject):
    result = docker_exec(KCONTAINER, [KCADM, 'get', f'users/{subject}', '--fields', 'id,username',
                                      '-r', 'jack', '--server', 'http://localhost:8080'])
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return data.get('username') if data.get('id') == subject else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--issuer', required=True, help='正式 issuer，例如 https://sso.example/realms/jack')
    parser.add_argument('--apply', action='store_true', help='真正写入绑定；默认只读预览')
    parser.add_argument('--container', default=CONTAINER, help='承载 kb-api 运行时的容器名')
    parser.add_argument('--map-file', default=str(HERE / MAP_FILE))
    args = parser.parse_args()

    issuer = args.issuer.rstrip('/')
    if not issuer.startswith('https://') or '/realms/' not in issuer:
        sys.exit('issuer 必须是 https 且形如 <base>/realms/<realm>：' + issuer)
    mapping = {name: row for name, row in json.load(open(args.map_file, encoding='utf-8')).items()
               if isinstance(row, dict)}
    if not mapping:
        sys.exit(f'{args.map_file} 里没有可绑定的账号')
    runtime = RUNTIME.get(args.container)
    if not runtime:
        sys.exit(f'未知容器 {args.container}，可用：{", ".join(RUNTIME)}')

    keys = read_env(KEYS_FILE)
    client_id, client_secret = keys.get('OIDC_ADMIN_CLIENT_ID'), keys.get('OIDC_ADMIN_CLIENT_SECRET')
    if not client_id or not client_secret:
        sys.exit(f'{KEYS_FILE} 缺少管理服务账号凭据')

    token = admin_token(args.container, issuer, client_id, client_secret, runtime)
    failures = 0
    for username, row in sorted(mapping.items()):
        remote = remote_username(row['kc_id'])
        if remote != username:
            print(f'  ! {username}: 身份中心该 UUID 的用户名是 {remote!r}，拒绝绑定')
            failures += 1
            continue
        command = [runtime, '/app/scripts/keycloak_accounts.py', '--issuer', issuer,
                   '--user-id', row['local_id'], '--subject', row['kc_id']]
        if args.apply:
            command.append('--apply')
        argv = ['sh', '-c',
                'read -r TOK; export OIDC_ADMIN_TOKEN="$TOK";'
                ' cd /app/services/kb-common && eval "$1"', 'sh',
                ' '.join(shlex.quote(part) for part in command)]
        result = docker_exec(args.container, argv, stdin=token + '\n')
        out = (result.stdout or '').strip().replace('\n', ' | ') or (result.stderr or '')[-200:]
        print(('  ✓ ' if result.returncode == 0 else '  ! ') + f'{username}: {out}')
        failures += result.returncode != 0

    if failures:
        print('存在失败项，未提交的部分请排查后重跑（脚本幂等）。')
        return 1
    print('身份绑定已提交，业务 User ID 与知识归属保持不变。' if args.apply
          else '预览通过，确认后加 --apply 写入。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
