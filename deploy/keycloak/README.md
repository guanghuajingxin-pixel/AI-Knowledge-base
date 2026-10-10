# 杰克科技统一身份中心

门户与知识治理专家共用 Keycloak Realm `jack`。账号、密码、启停和登录会话归 Keycloak 管理；业务数据库保留原 User ID、知识归属和钉钉绑定。FAQ 服务也使用同一验签与身份映射，避免子服务接受不同的账号体系。

> 内网机 10.10.166.2 的部署与切换（含老 CPU 镜像、平台 API Key 链路验证、回滚）见 [INTRANET-CUTOVER.md](INTRANET-CUTOVER.md)；`build/` 下的脚本就是那套步骤的可执行版本。上游镜像 `quay.io/keycloak/keycloak:26.7.4` 是 EL9 基线，在无 x86-64-v2 的机器上启动即 `Fatal glibc error`，需用 `build/build-on-host.sh` 重打镜像并以 `KEYCLOAK_IMAGE` 覆盖。


## 本机服务

```bash
python3 deploy/keycloak/prepare.py
docker compose -f deploy/keycloak/docker-compose.yml --env-file deploy/keycloak/.env up -d
python3 deploy/keycloak/bootstrap_admin.py
```

访问 http://localhost:8180 。`prepare.py` 生成随机管理员/数据库密码，写入仅本用户可读的 `.env`；不会覆盖现有文件。`bootstrap_admin.py` 仅用于本机，创建 `jack-admin` 统一管理员，密码位于 `.env` 的 `JACK_ADMIN_PASSWORD`；不覆盖已有账号密码。此账号的本机初始化邮箱是不可投递的 `localhost.invalid` 地址，请在实际使用前改为公司邮箱并配置 SMTP。

本次联调入口：门户 http://localhost:3200 、知识治理专家 http://localhost:3300 、统一管理 http://localhost:8180/admin/jack/console/ 。API/FAQ 分别运行于本机 8015/8016，复用本机业务数据库，仅为联调进程，不启动第二份调度任务。原有 3000/8000 进程及内网服务器尚未切换。

Realm 初始导入默认只允许门户 `http://localhost:3200/*`、知识治理 `http://localhost:3000/*`；本机初始化脚本额外注册 `http://localhost:3300/*` 供联调。生产应替换为实际的 HTTPS origin，不得使用通配主机。

## 应用接入

知识治理 API、FAQ 服务共用以下环境变量：

```dotenv
AUTH_PROVIDER=keycloak
OIDC_ISSUER=http://localhost:8180/realms/jack
OIDC_AUDIENCE=kb-api
OIDC_PORTAL_CLIENT_ID=jack-portal
OIDC_WEB_CLIENT_ID=knowledge-web
# 可选：API 容器不能访问公网 issuer 时指定内部 JWKS 地址；issuer 仍校验公网值。
# OIDC_JWKS_URL=http://keycloak:8080/realms/jack/protocol/openid-connect/certs
```

前端读取 API 的认证配置，不放 client secret、账号、密码。Keycloak JS 公开客户端使用授权码 + PKCE S256，令牌保存在内存。生产 API 与 FAQ 必须同时启用 Keycloak，不能只升级门户。门户 Nginx 的 `PORTAL_API_TARGET` 指向同一个知识治理 API origin。

本机联调可用以下命令（在相应服务目录运行）：

```bash
# services/kb-api；--lifespan off 仅用于联调，避免与原进程重复启动调度任务
AUTH_PROVIDER=keycloak OIDC_ISSUER=http://localhost:8180/realms/jack uv run uvicorn app.main:app --port 8015 --lifespan off
# services/faq-service
AUTH_PROVIDER=keycloak OIDC_ISSUER=http://localhost:8180/realms/jack uv run uvicorn app.main:app --port 8016 --lifespan off
# web
KB_API_TARGET=http://127.0.0.1:8015 FAQ_API_TARGET=http://127.0.0.1:8016 pnpm dev --port 3300
# portal/.env.local
# PORTAL_API_TARGET=http://127.0.0.1:8015
# VITE_KNOWLEDGE_URL=http://localhost:3300/chat
```

## 角色和账号管理

在 Keycloak「Users → 用户 → Role mapping」分配 **kb-api 客户端角色**：`viewer`、`editor`、`admin`、`super_admin`。应用只信任这一客户端下的角色；其他客户端或 Realm 同名角色不会提升应用权限。未分配任何业务角色的账号默认拒绝访问，不自动赋予管理员。

管理员在门户「企业账号 → 管理统一账号」或知识治理专家「用户管理 → 打开账号管理」维护账号。业务管理员与身份中心管理员是两个授权维度：业务 `admin`/`super_admin` 仅控制应用权限及入口可见性；操作 Keycloak 用户仍需单独授予 `realm-management` 中相应管理角色。本机初始化账号只授予 manage-users、view-users、query-users、view-clients、query-groups，不赋予 master Realm 管理权限。

统一认证启用后：

- 本地密码登录、改密、设密、钉钉直接签发 JWT、本地账号增删改停止工作，返回明确的身份中心引导。
- 旧本地 JWT 和旧 API Key 均不能访问接口；旧 API Key 不检查身份中心停用状态，因此不能作为绕过中央认证的入口。需要程序化访问时，应另行设计并授权服务账号。本次没有自动创建服务账号或扩大权限。
- 原钉钉身份绑定保持不变，继续用于知识检索身份映射；如需钉钉作为登录方式，应后续在身份中心配置身份代理。
- 停用用户、退出其他设备或更改角色后，已签发访问令牌最长仍有效 120 秒；刷新时生效。已开始的请求不被追溯撤销。前端每 15 秒检查是否需刷新，使用接口前也刷新令牌。
- 原本地 `is_active=false` 是额外的紧急封禁，不会被 SSO 自动恢复。中央恢复账号后如仍被拒绝，需管理员检查该本地封禁。

## 现有账号迁移

所有 schema 变更通过 Alembic：

```bash
cd services/kb-common
uv run alembic -c ../../alembic.ini heads
uv run alembic -c ../../alembic.ini upgrade head
```

`0052_user_oidc_identities` 新增 `(issuer, subject) → user_id` 映射，不修改业务用户 ID。首次 SSO 登录可创建新业务账号；同名本地账号必须明确绑定，**不按用户名或邮箱自动合并**。

迁移步骤：

1. 备份业务数据库与 Keycloak 数据库。
2. 使用 `uv run python ../../scripts/keycloak_accounts.py --export /安全目录/users.json` 导出账号清单（文件权限 600，不含密码或哈希，存在时拒绝覆盖）。
3. 在已创建的 `jack` Realm 使用 Partial import 导入 users。选择冲突时报错，不覆盖已有用户。保留 UUID；确认导入后的用户 ID，若导入路径重建了 ID，使用下述单账号绑定命令。
4. 在身份中心设置临时密码或通过已配置的公司 SMTP 发送改密邮件。原密码不复制、不冒充兼容；导入账号要求首次修改密码。
5. 获取短期身份中心管理员令牌，以环境变量 `OIDC_ADMIN_TOKEN` 提供。执行下面的只读计划，逐项确认后再 `--apply`：

```bash
uv run python ../../scripts/keycloak_accounts.py --issuer https://sso.example.com/realms/jack --bind-imported
uv run python ../../scripts/keycloak_accounts.py --issuer https://sso.example.com/realms/jack --bind-imported --apply
# 单个账号的明确绑定：
uv run python ../../scripts/keycloak_accounts.py --issuer https://sso.example.com/realms/jack --user-id LOCAL_UUID --subject KEYCLOAK_UUID --apply
```

脚本向身份中心核对远端 ID 和用户名；发生一对多/多对一绑定冲突时拒绝提交。业务角色从新令牌读取，不接受本地旧角色覆盖。迁移保留全部知识、原 User ID 和原钉钉身份关联。

6. 验证迁移管理员登录、知识归属、查看者权限和新用户流程后，将 **所有** API/FAQ 实例设置为 Keycloak 模式并部署新前端。不得让旧实例继续接受旧密码/令牌形成认证旁路。

## 正式部署

先确定三个稳定 HTTPS 地址：身份中心、门户、知识治理专家。内网裸 HTTP IP 不是浏览器安全上下文，不能直接使用 PKCE；不能通过关闭浏览器安全限制解决。调整 `.env` 的 origin 与 hostname，在新环境生成 realm import，再启动身份服务。TLS 可由已有反向代理终止，Keycloak HTTP 端口应仅允许可信反向代理访问；本配置默认绑定 loopback。`KC_PROXY_HEADERS=xforwarded` 要求入口覆盖转发头。

已有 Realm 不会因替换 import 文件而更新。更新客户端回调白名单等应通过管理控制台完成，禁止删除卷来重新初始化。数据库卷 `identity-db` 保存身份数据。配置 SMTP 后「忘记密码」和邀请邮件才可用。Keycloak 镜像固定 `26.7.4`，使用独立 PostgreSQL 16。

未接入范围：智能体测评、SkillHub 后端不在本次代码范围，它们的入口保留，尚无统一登录。

## 验证

```bash
cd services/kb-api && uv run python -m unittest discover -s tests -p test_oidc_auth.py -v
cd portal && pnpm type-check && pnpm test:run && pnpm build
cd web && pnpm type-check && pnpm test:run && pnpm build
```

协议基于 [Keycloak JavaScript adapter](https://www.keycloak.org/securing-apps/javascript-adapter)、[容器部署文档](https://www.keycloak.org/server/containers) 与 [Realm 导入说明](https://www.keycloak.org/server/importExport)。默认客户端 scope 包含 `basic`，用于在新版 Keycloak 访问令牌中提供 `sub`。
