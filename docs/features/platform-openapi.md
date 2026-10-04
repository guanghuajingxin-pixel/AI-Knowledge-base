# 平台知识库检索 OpenAPI

`POST /api/openapi/v1/knowledge/retrieve` 复用知识库检索实现，支持本地文档库、Dify、RAGFlow。Swagger 位于 `/docs`，OpenAPI 规范位于 `/openapi.json`，认证方案为 `PlatformAPIKey`。

## 统一凭证与授权

API Key 格式为 `client_id:client_secret`，通过 `X-API-Key` 发送。它是 Keycloak confidential client 凭证的封装，由平台身份中心统一管理，不在知识库或业务数据库重复保存。每个接入应用创建独立 client；同一凭证可调用所有获授权的平台能力。此版本实现知识库检索，其他平台 API 可复用 `kb_common.platform_auth`，并检查各自的资源角色。

凭证只用于服务端集成，不放入浏览器、URL、代码库或日志。生产使用 HTTPS。旧 `kb_` 用户 API Key 不升级、不接受；用户登录令牌和机器凭证保持独立，机器凭证不能调用管理接口。

管理员在现有 Keycloak Realm 中配置：

1. 在资源客户端 `kb-api` 新建 client role `knowledge:retrieve`。
2. 每个接入应用新建 OpenID Connect client，例如 `partner-search`。启用 Client authentication 和 Service accounts roles；关闭 Standard flow、Implicit flow 和 Direct access grants。
3. 将 `kb-api` 的 `knowledge:retrieve` 授予该客户端 Service account；关闭 Full scope allowed，将所需角色加入 role scope mappings。无需授予 admin、viewer 或 realm-management。
4. 配置 audience mapper，使 **access token** 的 `aud` 包含 `kb-api`。确认令牌包含 `resource_access.kb-api.roles` 中的 `knowledge:retrieve`。
5. 在 Credentials 获取 client secret，拼接 `partner-search:<secret>` 即为 API Key。轮换、删除 secret 或停用 client 均在 Keycloak 操作。
6. 在 API 服务配置环境变量并重启（默认白名单为空，拒绝所有机器客户端）：

```dotenv
AUTH_PROVIDER=keycloak
OIDC_ISSUER=https://sso.example.com/realms/jack
OIDC_AUDIENCE=kb-api
OIDC_API_CLIENTS=["partner-search"]
# 可选：容器内部 token endpoint，由部署管理员配置。
# OIDC_TOKEN_URL=http://keycloak:8080/realms/jack/protocol/openid-connect/token
# OIDC_JWKS_URL=http://keycloak:8080/realms/jack/protocol/openid-connect/certs
```

服务每次请求使用 client credentials grant 向身份中心验证当前凭证，不缓存 access token。然后校验签名、issuer、audience、有效期、token 类型及与该 API Key 一致的 azp，再检查资源角色。Keycloak 不可用时拒绝请求。停用/轮换在身份中心拒绝旧凭证后，于下次请求生效；若配置了旧 secret 的轮换宽限期，以 Keycloak 策略为准。已执行中的请求不追溯撤销。

实现参考：[Keycloak 服务账号与客户端凭证](https://www.keycloak.org/docs/26.8.0/server_admin/)。

## 调用

`PLATFORM_API_KEY` 由调用方安全注入，`library_ids` 使用知识库列表的整数 ID（不是底层 dataset ID），必须显式指定 1–24 个库。

```bash
curl -X POST 'https://knowledge.example.com/api/openapi/v1/knowledge/retrieve' \
  -H "X-API-Key: ${PLATFORM_API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"query":"设备维修流程","library_ids":[1,2],"top_k":8,"mode":"hybrid"}'
```

`mode` 支持 hybrid/vector/fulltext；可指定 rerank、similarity_threshold、vector_similarity_weight 等参数，详见 Swagger。`top_k` 为合并后最多返回条数，范围 1–50。未显式覆盖的本地库检索参数遵循库级设置。

```json
{
  "hits": [{"library_id":1,"library_name":"维修手册","document_id":"...","document_title":"维修流程","content":"...","score":0.85}],
  "total": 1,
  "elapsed_ms": 120,
  "partial": false,
  "failed_library_ids": []
}
```

授权范围：拥有 `knowledge:retrieve` 的应用可检索所有 enabled 的非素材知识库。这与当前平台按 enabled 开放检索的规则一致，**尚未实现逐应用知识库 ACL**。任一指定库不存在、未启用或为素材库，整次请求返回 403；空列表不会触发全库检索。

结果仅包含明确允许的字段，按 viewer 身份执行 search/post_output 脱敏；策略明确关闭时遵循配置，策略加载或执行异常时返回 503，不返回原文。不会向调用方返回底层配置、内部 URL 或异常堆栈。

错误：401 凭证无效，403 未授权，422 参数错误，502 全部库检索失败，503 鉴权/脱敏不可用。部分库失败返回 200，`partial=true` 和失败库 ID；无命中是成功的空结果。本地向量/重排故障的降级行为沿用现有检索实现。

## 验证与上线

```bash
cd services/kb-api
uv run python -m unittest discover -s tests -p 'test_open_api.py'
uv run python -m unittest discover -s tests -p 'test_oidc_auth.py'
```

无需数据库迁移。上线需完成上述 Keycloak 配置，并以真实应用凭证验证正常检索、撤销角色、停用客户端和 secret 轮换。本次自动测试使用模拟身份中心，未修改现有 Realm 或创建真实生产凭证。
