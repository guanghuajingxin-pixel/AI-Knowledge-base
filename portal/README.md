# 杰克科技 AIGC 门户

独立的 Vue 3 + TypeScript + Element Plus 应用。首页为「小杰」智能助手，左侧整合知识治理专家、智能体测评与 SkillHub。可单独构建和部署，不修改现有知识治理系统。

## 本地启动

```bash
cd portal
pnpm install
cp .env.example .env
pnpm dev
```

访问 http://localhost:3200 。需要能访问 10.10.166.2 的内网环境。

```bash
pnpm type-check
pnpm test:run
pnpm build
```

## 统一账号与身份

门户与知识治理专家共享同一后端认证配置（`GET /api/v1/auth/config`）。

- `AUTH_PROVIDER=keycloak`：跳转杰克科技统一身份中心，采用授权码 + PKCE S256；门户与知识治理专家使用同一个 Realm，各自使用公开客户端。登录一次后，另一应用复用 SSO 会话；不会传递密码或跨窗口复制令牌。
- `AUTH_PROVIDER=local`：兼容知识治理专家原有账号，由真实后端校验；门户不再有独立账号或前端口令校验。此模式只共享账号数据，不提供跨应用 SSO。
- 小杰使用本次登录的访问令牌调用企业问答，无需二次登录。令牌仅存于内存，刷新通过身份中心恢复会话；过期前自动刷新。
- 门户底部「企业账号」可进入个人账号中心，管理员可进入统一账号管理。中心支持新增/停用用户、分配角色、重置密码与会话管理。
- 在 Keycloak 模式下，知识治理专家以独立窗口打开，避免身份中心登录页被 iframe 限制。测评与 SkillHub 仍保持原入口，尚未接入统一认证。

部署、角色映射、账号迁移和验证步骤见 [统一身份中心说明](../deploy/keycloak/README.md)。模型和知识权限仍由知识治理专家后端执行。

## 应用入口

| 应用 | 默认地址 | 构建环境变量 |
| --- | --- | --- |
| 知识治理专家 | http://10.10.166.2:8080/chat | `VITE_KNOWLEDGE_URL` |
| 智能体测评 | http://10.10.166.2:8082/datasets | `VITE_EVALUATION_URL` |
| SkillHub | http://10.10.166.2:3100/ | `VITE_SKILLHUB_URL` |

入口使用 hash 路由，支持刷新、浏览器前进后退与直接链接。默认在 iframe 中打开，始终提供「新窗口打开」。目标应用的 CSP / X-Frame-Options、Cookie 策略可能阻止嵌入或登录；父页面不能可靠判断跨域 iframe 是否成功显示。门户使用 HTTPS 而应用使用 HTTP 时，直接显示新窗口入口，避免混合内容空白。

## 独立部署

```bash
cd portal
docker compose up -d --build
```

默认端口 `3200`，不占用现有三个应用端口。Nginx 同源代理 `/api/v1/`，无需把模型密钥放到前端。`PORTAL_API_TARGET` 可运行时配置，必须是可信知识治理服务的 origin，不带结尾斜线。需要改变应用链接时，在构建环境传入 VITE 变量或修改 `src/config/apps.ts` 后重新构建。

如不使用 Docker，将 `dist/` 部署到 Nginx 并按 `nginx.conf.template` 配置 API 代理。`pnpm preview` 仅用于检查静态产物，不代理问答服务。生产环境建议统一配置 HTTPS，包括被嵌入的应用。
