# 10.10.166.2 统一身份中心上线与切换手册

> 状态：身份服务已在内网机跑通并完成链路验证，**人类登录仍是本地模式**（按 2026-10-09 的决定「先只上身份服务，晚点再切」）。
> 平台 API Key 页面（`/settings/api-keys`）现在仍会报「请先启用 Keycloak 统一认证」，因为签发与校验都以 `AUTH_PROVIDER=keycloak` 为门禁；切换它就是把下面第 3 节的开关翻开。

## 1. 当前已完成

| 项 | 位置 / 值 |
|----|-----------|
| 身份服务 | `jack-identity-keycloak-1` + `jack-identity-database-1`，compose：`/opt/kge/deploy/keycloak/docker-compose.yml` |
| 镜像 | `kge-keycloak:26.7.4-v1`（本机老 CPU 专用，见第 4 节坑 1） |
| 监听 | `127.0.0.1:8180`（未开公网入口，等 HTTPS 证书） |
| Realm | `jack`，客户端 `knowledge-web` / `jack-portal` / `kb-api`（资源客户端，含 `knowledge:retrieve` 与四个业务角色） |
| 管理服务账号 | 机密客户端 `platform-key-manager`，凭据在 `/opt/kge/deploy/intranet/platform-keys.env`（600） |
| 业务账号 | `admin`(super_admin)、`黄景新/夏迎/李伟` 对应的 3 个 `dd_*`(viewer)，姓名取自 `dingtalk_bindings.dt_name` |
| 一次性密码 | `/opt/kge/deploy/keycloak/initial-passwords.env`（600），首登强制改密 |
| 身份映射表 | `/opt/kge/deploy/keycloak/account-map.json`（业务 UUID ↔ Keycloak UUID） |
| 链路验证 | 一次性实例全绿：签发 → 回显 → OpenAPI 检索 200 → 伪造 Key 401 → 页面停用后 401 |

脚本都在 `deploy/keycloak/build/`：`build-on-host.sh`（重打镜像）、`provision-platform-keys.sh`（角色 + 管理服务账号 + basic scope）、`provision-accounts.py`（建号授权）、`verify-platform-keys.sh`（端到端验证，跑完自动清理）。

## 2. 需要你先给的东西

1. 证书文件（fullchain + key，含中间 CA）放到 `/etc/nginx/ssl/` 下，告诉我路径。
2. 三个稳定 HTTPS 地址（必须在证书 SAN 内）：身份中心、知识治理、AIGC 门户。
3. 内网 DNS 是否能解析这些域名；不能解析的话，切换要等 hosts 下发，否则用户直接打不开。

`prepare.py` 里的 `origin()` 会拒绝「非 localhost 的 http 地址」，这不是保守，是浏览器不给裸 HTTP 的 IP 提供 `crypto.subtle`，PKCE 走不了（坑 5）。

## 3. 切换步骤（建议低峰期，10 分钟内完成）

```bash
# 0) 备份（回滚要用）
ssh jack@10.10.166.2
cp /opt/kge/deploy/intranet/.env.app /opt/kge/deploy/intranet/.env.app.bak-$(date +%F-%H%M)
docker exec kge-postgres pg_dump -U dev dev_db > /opt/kge/backup/pre-sso-$(date +%F).sql

# 1) Keycloak 换成公网 HTTPS 地址并重启
vi /opt/kge/deploy/keycloak/.env        # KEYCLOAK_HOSTNAME=https://sso.<域名>
cd /opt/kge/deploy/keycloak && docker compose up -d keycloak

# 2) 回调地址改成真实 origin（已导入的 realm 不会因替换 import 文件而更新，只能改对象）
#    每个公开客户端：redirectUris / webOrigins / post.logout.redirect.uris
docker exec -i jack-identity-keycloak-1 /opt/keycloak/bin/kcadm.sh update clients/<id> \
  -s 'redirectUris=["https://kge.<域名>/*"]' -s 'webOrigins=["https://kge.<域名>"]' \
  -r jack --server http://localhost:8080
# 再跑一次 provision-platform-keys.sh，确保 basic scope 与角色在位

# 3) 绑定三个钉钉账号（admin 已在验证时绑过，重复执行是幂等的）
#    issuer 必须是最终 HTTPS 值，否则映射键与应用配置不一致
python3 /home/jack/kc-build-scripts/bind-accounts.py --issuer https://sso.<域名>/realms/jack
python3 /home/jack/kc-build-scripts/bind-accounts.py --issuer https://sso.<域名>/realms/jack --apply
# 兜底（等价命令，逐个账号）：
#   docker exec -i kge-kb-api sh -c 'read -r T; export OIDC_ADMIN_TOKEN=$T;'
#     'cd /app/services/kb-common && /app/services/kb-api/.venv/bin/python'
#     '/app/scripts/keycloak_accounts.py --issuer https://sso.<域名>/realms/jack'
#     '--user-id <业务UUID> --subject <KeycloakUUID> --apply'
#   两个 UUID 都在 /opt/kge/deploy/keycloak/account-map.json 里

# 4) 应用侧翻开关：kb-api + faq-service 同时生效，不能只升一个
cd /opt/kge/deploy/intranet && vi .env.app
#   AUTH_PROVIDER=keycloak
#   OIDC_ISSUER=https://sso.<域名>/realms/jack
#   OIDC_AUDIENCE=kb-api
#   OIDC_PORTAL_CLIENT_ID=jack-portal
#   OIDC_WEB_CLIENT_ID=knowledge-web
#   OIDC_JWKS_URL=http://127.0.0.1:8180/realms/jack/protocol/openid-connect/certs   # 容器不可达公网 issuer 时加
#   OIDC_TOKEN_URL=http://127.0.0.1:8180/realms/jack/protocol/openid-connect/token
#   OIDC_ADMIN_URL=http://127.0.0.1:8180/admin/realms/jack
#   OIDC_ADMIN_CLIENT_ID / OIDC_ADMIN_CLIENT_SECRET ← platform-keys.env 两行合并进来
docker compose -f docker-compose.app.yml up -d kb-api kb-worker faq-service

# 5) 验证：先脚本后浏览器
ISSUER=https://sso.<域名>/realms/jack bash /home/jack/kc-build-scripts/verify-platform-keys.sh
#   浏览器：https://kge.<域名>/ 统一登录 → 首登改密 → 平台配置 → API Key → 签发一个 → curl OpenAPI
```

第 4 步生效后，`/settings/api-keys` 立刻可用，不需要再改代码。

## 4. 实测踩到的坑（新环境按此顺序检查）

1. **老 CPU 跑不动官方镜像**：`quay.io/keycloak/keycloak:26.7.4` 是 EL9 基线，Core2 Duo T7700 缺 x86-64-v2，启动即 `Fatal glibc error`。解法：`build-on-host.sh` 把上游发行包套到 `eclipse-temurin:17-jdk`（Ubuntu 基线）重打，compose 用 `KEYCLOAK_IMAGE` 覆盖。
2. **令牌没有 `sub`**：新版 Keycloak 的 `sub` 由 `basic` 客户端 scope 提供，旧 `import/jack-realm.json` 的默认 scope 里没有它，`kb-api` 的 `require_sub` 会把正常登录判成 401。`prepare.py` 生成的新 import 已带 `basic`（`import/` 不入库，按环境生成）；已经导入过的 realm 只能靠 `provision-platform-keys.sh` 补挂 scope，替换文件不会生效。
3. **`Account is not fully set up`**：Keycloak 判定账号「配置完整」要求 `firstName`+`lastName` 非空。只照搬业务库用户名建号会全员卡死，`provision-accounts.py` 已用 `dingtalk_bindings.dt_name` 填姓名。
4. **一次性密码不能走非浏览器 password grant**：带 `UPDATE_PASSWORD` 或 temporary 凭据的账号，用 `grant_type=password` 取令牌会被拒。`verify-platform-keys.sh` 只在验证期间临时改成非一次性密码，退出时恢复一次性密码并重新写入密码文件，同时关闭 `knowledge-web` 的密码直连开关。
5. **裸 IP HTTP 不是安全上下文**：`web/src/auth/sso-client.ts` 在非安全上下文直接抛「统一登录需要 HTTPS」。必须 HTTPS，不要靠关浏览器安全限制绕过。
6. **bootstrap 管理员只认容器内 loopback**：`KC_BOOTSTRAP_ADMIN_*` 建的是临时管理员，宿主机通过发布端口调用会被拒。管理令牌要在容器内从 kcadm 缓存取（脚本已这么处理）。
7. **切换即失效的入口**：本地密码登录、钉钉直发 JWT、旧 `kb_` 用户 API Key、旧登录令牌全部停用；这是设计意图（避免绕过身份中心），所以第 3 节第 3 步的账号绑定必须先做完再翻开关。
8. **业务库紧急封禁仍有效**：本地 `users.is_active=false` 是额外封禁，SSO 不会自动恢复；恢复账号后仍被拒就查这一位。
9. 老机器 containerd/LVM 偶发 `tmpmounts busy`，`docker build`/`pull` 失败时先 `umount -l /data/containerd/tmpmounts/*` + `docker builder prune -f` 再重试（脚本内置 6 次重试）。

## 5. 回滚

```bash
cd /opt/kge/deploy/intranet
cp .env.app.bak-<时间戳> .env.app          # AUTH_PROVIDER 回到 local
docker compose -f docker-compose.app.yml up -d kb-api kb-worker faq-service
```

回滚后本地密码登录立刻恢复（业务库凭据未被改动）。代价：Keycloak 模式下签发的平台 API Key 在本地模式不被接受，需要重新接入时再切回来。若只要停掉身份服务而不影响业务：`docker compose -f /opt/kge/deploy/keycloak/docker-compose.yml stop`（`identity-db` 卷保留身份数据，禁止删卷重建）。

## 6. 还没接的部分

- 智能体测评、SkillHub 后端的统一登录不在本次范围，入口保留。
- 钉钉作为登录方式需要在身份中心配身份代理（本次只把钉钉姓名用于补全账号资料）。
- 逐应用知识库 ACL 未实现：持 `knowledge:retrieve` 的应用可检索所有 enabled 非素材库。
