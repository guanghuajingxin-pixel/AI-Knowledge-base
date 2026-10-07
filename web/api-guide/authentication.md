# 鉴权与 API Key

## 在平台签发 Key

1. 使用管理员账号进入 **平台配置 → API Key**。
2. 点击 **签发 Key**，填写调用方应用名称，例如「售后服务系统」。
3. 确认访问范围，完成签发。当前权限为 **知识库检索**，可检索所有已启用的非素材知识库。
4. 复制完整密钥，安全保存到调用方服务端的密钥管理系统或环境变量。

::: warning 仅显示一次
完整 API Key 仅在签发完成时显示。列表不会再次回显。遗失时请签发新的 Key，更新调用方配置后删除旧 Key。
:::

## 统一鉴权

平台 API Key 由 Keycloak 应用凭证构成，格式为 `client_id:client_secret`。调用时完整放入 `X-API-Key` 请求头，不要拆分，也不要加 `Bearer` 前缀。

```http
X-API-Key: platform-api-<应用标识>:<密钥>
Content-Type: application/json
```

每个接入应用使用自己的 Key。同一 Key 可跨当前授权范围内的知识库使用，无需为每个库另行申请。

| 接口类型 | 使用凭证 | 权限要求 |
| --- | --- | --- |
| 知识库检索 OpenAPI | `X-API-Key` | `knowledge:retrieve` |
| API Key 管理 | 用户登录令牌 | admin / super_admin |
| 文档解析契约 | 用户登录令牌 | admin / super_admin |

机器凭证不能用于登录平台或调用管理接口。旧版 `kb_` 个人 API Key 不适用于本页检索接口。

## 第一次调用

由你的运行环境安全注入 `PLATFORM_API_KEY`，将 `BASE_URL` 和知识库 ID 替换为实际值。Base URL 是知识治理平台的地址，不是身份中心地址。

```bash
BASE_URL='https://knowledge.example.com'
curl --fail-with-body "${BASE_URL}/api/openapi/v1/knowledge/retrieve" \
  -H "X-API-Key: ${PLATFORM_API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"query":"设备维修流程","library_ids":[1],"top_k":8}'
```

完整字段、响应示例和错误处理见 [知识库检索](./retrieval.md)。

## 停用与删除

- **停用**：列表关闭启用开关，确认后阻止该应用后续调用；可再次启用。
- **删除**：在行内「更多」中删除，确认后凭证永久失效，无法恢复。
- **重命名**：修改应用显示名称，不影响现有 Key。

接口每次向身份中心校验当前凭证，不缓存访问令牌。停用或删除在身份中心变更成功后影响下一次调用，已在执行的请求不追溯取消。

::: tip 配置提示
若页面提示密钥管理服务账号未配置，请由部署管理员完成 [身份中心接入](./administration.md)。不要用本地个人 Key 代替平台 Key。
:::
