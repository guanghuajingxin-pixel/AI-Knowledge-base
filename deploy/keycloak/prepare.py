"""Create a realm import; never overwrite an existing realm or local secrets.

Run from anywhere: python3 deploy/keycloak/prepare.py
The initial application administrator is created in Keycloak Admin Console.
"""
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent


def origin(value):
    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise ValueError('Use an exact origin without path/query/fragment')
    if parsed.scheme != 'https' and parsed.hostname not in ('localhost', '127.0.0.1'):
        raise ValueError('Non-local origins require HTTPS for PKCE')
    return value.rstrip('/')


def realm_config(portal, knowledge):
    clients = []
    for client_id, app_origin in [('jack-portal', origin(portal)), ('knowledge-web', origin(knowledge))]:
        clients.append({
            'clientId': client_id, 'name': '杰克 AIGC 门户' if client_id == 'jack-portal' else '知识治理专家',
            'protocol': 'openid-connect', 'publicClient': True, 'standardFlowEnabled': True,
            'directAccessGrantsEnabled': False, 'implicitFlowEnabled': False,
            'serviceAccountsEnabled': False, 'fullScopeAllowed': True,
            'redirectUris': [app_origin + '/*'], 'webOrigins': [app_origin],
            'attributes': {'pkce.code.challenge.method': 'S256', 'post.logout.redirect.uris': app_origin + '/*'},
            'defaultClientScopes': ['basic', 'web-origins', 'roles', 'profile', 'email'],
            'protocolMappers': [{
                'name': 'kb-api-audience', 'protocol': 'openid-connect',
                'protocolMapper': 'oidc-audience-mapper',
                'config': {'included.client.audience': 'kb-api', 'access.token.claim': 'true', 'id.token.claim': 'false'},
            }],
        })
    clients.append({'clientId': 'kb-api', 'name': '知识治理 API 权限', 'protocol': 'openid-connect',
                    'bearerOnly': True, 'standardFlowEnabled': False, 'directAccessGrantsEnabled': False})
    return {
        'realm': 'jack', 'displayName': '杰克科技 · 统一身份中心', 'enabled': True,
        'sslRequired': 'external', 'registrationAllowed': False, 'loginWithEmailAllowed': True,
        'duplicateEmailsAllowed': False, 'resetPasswordAllowed': True, 'rememberMe': True,
        'bruteForceProtected': True, 'accessTokenLifespan': 120,
        'ssoSessionIdleTimeout': 1800, 'ssoSessionMaxLifespan': 28800,
        'revokeRefreshToken': True, 'refreshTokenMaxReuse': 0,
        'internationalizationEnabled': True, 'supportedLocales': ['zh-CN', 'en'], 'defaultLocale': 'zh-CN',
        'passwordPolicy': 'length(12) and digits(1) and upperCase(1) and lowerCase(1)',
        'clients': clients,
        'roles': {'client': {'kb-api': [
            {'name': 'super_admin', 'description': '知识治理超级管理员'},
            {'name': 'admin', 'description': '知识治理管理员'},
            {'name': 'editor', 'description': '知识编辑者'},
            {'name': 'viewer', 'description': '知识查看者'},
        ]}},
        # No default application role or admin grant: assign explicitly after creating accounts.
    }


def main():
    env_path = ROOT / '.env'
    if not env_path.exists():
        content = (ROOT / '.env.example').read_text().replace('replace-with-random-secret', secrets.token_urlsafe(32)).replace('replace-with-another-random-secret', secrets.token_urlsafe(32))
        with env_path.open('x') as file:
            os.chmod(env_path, 0o600)
            file.write(content)
    env = dict(line.split('=', 1) for line in env_path.read_text().splitlines() if line and not line.startswith('#'))
    data = realm_config(os.getenv('PORTAL_ORIGIN', env['PORTAL_ORIGIN']), os.getenv('KNOWLEDGE_ORIGIN', env['KNOWLEDGE_ORIGIN']))
    output = ROOT / 'import' / 'jack-realm.json'
    output.parent.mkdir(exist_ok=True)
    with output.open('x') as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    print('已生成独立身份服务配置；管理员凭据保存在 deploy/keycloak/.env（未打印）。')


if __name__ == '__main__':
    main()
