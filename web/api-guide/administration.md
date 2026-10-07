# 身份中心接入

本页供部署管理员配置前端签发能力。业务管理员只操作「平台配置 → API Key」，不会接触身份中心管理凭证。

## 一次性配置

1. 在现有 Realm 的资源客户端 `kb-api` 下创建客户端角色 `knowledge:retrieve`。
2. 新建 confidential client `platform-key-manager`，启用 Client authentication 和 Service accounts roles，关闭标准登录、隐式登录和密码登录。
3. 为其服务账号配置 Keycloak Admin REST 权限，覆盖创建/查看/更新/删除客户端，以及为新客户端服务账号分配 `kb-api` 的检索角色和 scope mapping。常规 Realm 配置可使用 realm-management 的 `manage-clients`、`view-clients`、`query-clients`、`manage-users`、`view-users`；如启用细粒度管理权限，应限制到平台机器客户端及检索角色。
4. 在 kb-api 服务端环境变量保存该管理客户端的凭证。它是服务端管理凭证，不是对外 API Key，不能放入前端。

```dotenv
AUTH_PROVIDER=keycloak
OIDC_ISSUER=https://sso.example.com/realms/jack
OIDC_AUDIENCE=kb-api
OIDC_ADMIN_CLIENT_ID=platform-key-manager
OIDC_ADMIN_CLIENT_SECRET=<管理客户端密钥>
# 容器网络需要时设置内部地址：
# OIDC_ADMIN_URL=http://keycloak:8080/admin/realms/jack
# OIDC_TOKEN_URL=http://keycloak:8080/realms/jack/protocol/openid-connect/token
# OIDC_JWKS_URL=http://keycloak:8080/realms/jack/protocol/openid-connect/certs
```

重启 API 服务，然后从「平台配置 → API Key」签发测试应用，调用检索接口并验证停用后返回 401。不要为管理客户端分配 `knowledge:retrieve`，也不要将其加入外部调用白名单。

## 签发行为

平台通过 Keycloak Admin REST 创建独立服务客户端，自动完成：

- 分配唯一应用标识 `platform-api-…`。
- 关闭用户交互登录，仅允许客户端凭证方式认证。
- 分配 `knowledge:retrieve` 服务账号角色及对应 scope mapping。
- 配置资源 audience 和签名令牌中的平台应用标识声明。
- 授权配置完成后才启用客户端，读取密钥并向签发页返回一次。

平台不保存客户端密钥副本。列表仅返回应用名称、标识、状态、签发时间和权限；操作审计不包含密钥。对不是平台签发的 Keycloak 客户端，管理 API 拒绝更新和删除。

页面签发的应用即时生效，无需逐个添加环境变量白名单或重启。先前手动创建的集成客户端仍可通过 `OIDC_API_CLIENTS` 配置兼容，但不会出现在平台签发列表中。

## 故障处理

| 提示 | 检查项 |
| --- | --- |
| 需要启用 Keycloak | AUTH_PROVIDER 是否为 keycloak |
| 未配置密钥管理服务账号 | 管理 client ID、secret 是否配置并重启 |
| 服务账号无法认证 | 管理客户端启用状态、凭证和 token endpoint |
| 服务账号权限不足 | Admin REST 权限、scope mapping 权限 |
| 应用或检索角色不存在 | kb-api 资源客户端与 knowledge:retrieve 是否已创建 |
| 身份中心暂不可用 | 网络连通性、TLS、身份中心运行状态 |

由于跨系统操作没有数据库事务，网络超时时可能遗留尚未完成签发的停用客户端；管理员可在身份中心根据 `platform.api.managed` 属性核对并清理。若签发响应丢失，原密钥不会补发，应检查列表后重新签发并撤销不再使用的应用。

## 文档构建

本说明站使用 **VitePress 1.6.4** 默认主题，并配置中文本地搜索、章节目录、代码复制和品牌色。所有资源随构建产物部署，无 CDN 依赖。

```bash
cd web
pnpm docs:dev    # 单独编辑文档
pnpm docs:build  # 输出到 public/api-guide
pnpm build      # 自动先构建文档，再构建主站，统一输出到 dist
```

主站入口为 `/api-guide/`。旧 `/mineru-api-docs.html` 地址自动跳转到新站解析章节。参考页面因需要登录未能确认原框架，本项目经确认选择 VitePress，并未声称复用了参考站源码或主题。
