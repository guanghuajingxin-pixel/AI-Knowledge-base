# 解析引擎 · API 调用文档

统一契约 · 本地 Kit / 云端 SaaS 双引擎 · JWT 鉴权（admin / super_admin）
本地引擎地址：`http://127.0.0.1:8010` · [本地 Kit Swagger →](http://127.0.0.1:8010/docs) · kb-api 契约 OpenAPI：`http://<kb-api>:8000/docs`（FastAPI 自动生成，schema 见 `mineru_contract.py`）

> 本文档对应 kb-api **统一契约**（schema 见 `services/kb-api/app/routes/mineru_contract.py`，kb-api `/docs` 可查）。调用方只认这一套端点，本地/云端差异仅体现在 `X-Mineru-Base` 请求头与 capabilities，不再依赖 MinerU 原生 V1 细节。

<a id="global"></a>

## 全局约定

| 条目 | 说明 |
| --- | --- |
| Base URL | kb-api 前缀 `/api/v1/mineru`；下文各接口示例均给出**完整字面 URL**（kb-api 默认 `http://127.0.0.1:8000`），复制后替换成实际地址即可 |
| 鉴权 | 全部端点需 JWT：`Authorization: Bearer <token>`，且角色为 **admin / super_admin**；获取方式见[鉴权与获取 Token](#auth) |
| 引擎选择 | 请求头 `X-Mineru-Base`：本地引擎填 Base URL（默认 `http://127.0.0.1:8010`）；云端 SaaS 填 `cloud` |
| 直接复制运行 | 每条 curl 都自带全部请求头，**只需把 `<你的JWT>` 替换为登录返回的 `access_token`** 即可直接执行 |
| 产物格式 | 统一 `markdown` + `structured_json`（本地 middle.json / 云端 content_list.json 归入同一槽位） |
| 档位 | 语义三级 `speed` / `balanced` / `quality`（也接受原始档位 flash/standard/advanced/pipeline/vlm，未知值回退 balanced）；响应含 `tier`（语义）与 `raw_tier`（引擎原值） |
| 错误结构 | kb-api 校验失败返回 `{"detail":"..."}`；本地引擎上游错误原样透传（`{"error":{"message":"..."}}`） |

## 两种引擎接入

| 维度 | 本地引擎（mineru-kit） | 云端 SaaS（MinerU 云 V4） |
| --- | --- | --- |
| `X-Mineru-Base` | 引擎 Base URL（默认 `http://127.0.0.1:8010`，页面顶部可配） | 固定 `cloud` |
| 密钥 | 无需 | 模型配置页 `mineru_api_key`（服务端保存，前端不经手） |
| 档位原始值 | flash / standard / advanced | pipeline / vlm |
| 取消任务 | 支持（DELETE） | 不支持（返回 409，`capabilities.cancelable=false`） |
| 页码范围 page_range | 支持 | 支持 |
| 图片形态 | markdown 内 base64 自包含，直接下 `.md` | 图片在 zip 内 `images/` 目录，经 result-zip 下载 |
| 任务注册表 | 引擎服务持有 | kb-api 进程内临时态（kb-api 重启清空） |

能力发现以 `GET /v1/health` 返回的 `capabilities` 为准（engine / tiers / output_formats / cancelable / page_range / max_file_mb），调用方按能力适配而非按引擎适配。

<a id="auth"></a>

## 鉴权与获取 Token

::: warning Keycloak 统一登录模式
启用 Keycloak 时，本地 `/api/v1/auth/login` 不签发令牌。应使用统一身份中心授权码 + PKCE 登录取得用户 access token，并具有知识治理 admin / super_admin 角色。下方本地密码登录示例仅用于 `AUTH_PROVIDER=local`。

解析接口目前不接受平台 API Key；平台 Key 用于 [知识库检索](./retrieval.md)。
:::


解析引擎契约端点**全部需要 JWT**，且角色为 admin / super_admin（普通用户返回 403）。JWT 通过登录接口获取，之后放在 `Authorization: Bearer <token>` 请求头中随每个请求发送。

### 登录接口（POST /api/v1/auth/login）

> 注意前缀：登录是 kb-api 通用接口，**不带** `/mineru` 段。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `username` | string | 是 | 用户名（初始管理员 admin，密码见部署种子脚本，登录后可在「用户管理」修改） |
| `password` | string | 是 | 密码 |

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `access_token` | string | **JWT 令牌**，后续请求放进 `Authorization: Bearer <access_token>` |
| `token_type` | string | 固定 `bearer` |
| `user` | object | 用户公开信息（id / username / role 等） |
| `must_set_password` | bool | 首次登录是否需改密 |

Token 有效期默认 **24 小时**（`JWT_TTL_MINUTES`，可配）；过期后接口返回 401，重新登录换取即可。

请求示例（curl，可直接执行）：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}'
# → {"access_token":"eyJhbGciOi...","token_type":"bearer","user":{...},"must_set_password":false}
```

```python
import requests

r = requests.post("http://127.0.0.1:8000/api/v1/auth/login",
                  json={"username": "admin", "password": "admin123"})
r.raise_for_status()
token = r.json()["access_token"]
```

拿到 token 后，所有解析引擎接口统一加两个头（下文 curl 示例均已带全）：

```bash
-H 'Authorization: Bearer <你的JWT>' \
-H 'X-Mineru-Base: http://127.0.0.1:8010'    # 云端 SaaS 改为 -H 'X-Mineru-Base: cloud'
```

<a id="quickstart"></a>

## 快速开始（60 秒跑通）

一次完整的解析 = **取 JWT** → **三步上传** 得 `file_id` → **创建解析任务** → **轮询** 至终态 → **下载产物**（变量式完整流程，自上而下直接执行）：

```bash
KB=http://127.0.0.1:8000                      # kb-api 地址
XB='X-Mineru-Base: http://127.0.0.1:8010'     # 本地引擎；云端 SaaS 改为 'X-Mineru-Base: cloud'

# ① 登录换 JWT（详见「鉴权与获取 Token」）
TOKEN=$(curl -s -X POST $KB/api/v1/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')
AUTH="Authorization: Bearer $TOKEN"

# ② 创建上传会话
curl -s -X POST $KB/api/v1/mineru/v1/uploads -H "$AUTH" -H "$XB" \
  -H 'Content-Type: application/json' -d '{"filename":"demo.pdf","bytes":123456,"mime_type":"application/pdf"}'
# → {"id":"upload_xxx"}

# ③ PUT 原始字节
curl -s -X PUT $KB/api/v1/mineru/v1/uploads/upload_xxx/content -H "$AUTH" -H "$XB" \
  -H 'Content-Type: application/pdf' --data-binary @demo.pdf
# → {"ok":true}

# ④ 完成上传 → file_id
curl -s -X POST $KB/api/v1/mineru/v1/uploads/upload_xxx/complete -H "$AUTH" -H "$XB"
# → {"id":"upload_xxx","file":{"id":"file-yyy","filename":"demo.pdf"}}

# ⑤ 创建解析任务
curl -s -X POST $KB/api/v1/mineru/v1/parse/jobs -H "$AUTH" -H "$XB" -H 'Content-Type: application/json' \
  -d '{"files":[{"source":{"type":"file_id","file_id":"file-yyy"}}],
       "ocr_mode":"auto","output_formats":["markdown","structured_json"],"tier":"balanced"}'
# → {"job_id":"job_zzz","status":"queued","tier":"balanced",...}

# ⑥ 轮询任务直至终态（completed / partial / failed / canceled）
curl -s $KB/api/v1/mineru/v1/parse/jobs/job_zzz -H "$AUTH" -H "$XB"

# ⑦ 下载 markdown 产物（文件原文）
curl -s $KB/api/v1/mineru/v1/files/file-mdout/content -H "$AUTH" -H "$XB" -o result.md
```

> curl 与 Python（requests）两种完整调用脚本见[端到端示例](#e2e)；浏览器**无法直连**引擎（不带 CORS 头），前端统一走本契约（同源 `/api/v1/mineru/*`），见[前端集成](#frontend)。

<a id="pycommon"></a>

## 调用准备（Python 示例公共头）

以下每个接口的 Python 示例都基于这段公共代码（含登录换 token）：

```python
import requests

KB = "http://127.0.0.1:8000"        # kb-api 地址
ENGINE = "http://127.0.0.1:8010"    # 本地引擎 Base URL；云端 SaaS 改为 "cloud"

# 登录换 JWT（admin / super_admin）
token = requests.post(f"{KB}/api/v1/auth/login",
                      json={"username": "admin", "password": "admin123"}).json()["access_token"]

def hdr(extra=None):
    h = {"Authorization": f"Bearer {token}", "X-Mineru-Base": ENGINE}
    h.update(extra or {})
    return h

API = f"{KB}/api/v1/mineru"
```

<a id="health"></a>

## 健康检查（GET /v1/health）

连接测试 + 引擎能力发现。无请求参数。

```bash
curl -s http://127.0.0.1:8000/api/v1/mineru/v1/health \
  -H 'Authorization: Bearer <你的JWT>' \
  -H 'X-Mineru-Base: http://127.0.0.1:8010'
```

```python
r = requests.get(f"{API}/v1/health", headers=hdr())
cap = r.json()["capabilities"]      # {"engine":"local","cancelable":True,...}
```

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `version` | string | 引擎版本（本地如 `1.x.x`；云端 `cloud-v4 (SaaS)`） |
| `features.output_formats` | string[] | 支持的产物格式，统一含 `markdown` / `structured_json` |
| `capabilities.engine` | string | `local` / `cloud` |
| `capabilities.engine_label` | string | 引擎显示名（如 `MinerU Kit Local`） |
| `capabilities.tiers` | string[] | 可用语义档位 |
| `capabilities.output_formats` | string[] | 可用产物格式 |
| `capabilities.cancelable` | bool | 是否支持取消任务（云端 false） |
| `capabilities.page_range` | bool | 是否支持页码范围 |
| `capabilities.max_file_mb` | int | 单文件大小上限（MB） |

响应示例：

```json
{
  "version": "1.x.x",
  "features": {"output_formats": ["markdown", "structured_json"]},
  "capabilities": {
    "engine": "local", "engine_label": "MinerU Kit Local",
    "tiers": ["speed", "balanced", "quality"],
    "output_formats": ["markdown", "structured_json"],
    "cancelable": true, "page_range": true, "max_file_mb": 200
  }
}
```

> 云端 SaaS 模式未配置 MinerU API Key 时返回 503：请在「模型配置」页填写并保存后再测。

<a id="tiers"></a>

## 语义档位（GET /v1/tiers）

返回统一语义三级档位及当前引擎下的原始档位值，创建任务时作为 `tier` 传入。无请求参数。

```bash
curl -s http://127.0.0.1:8000/api/v1/mineru/v1/tiers \
  -H 'Authorization: Bearer <你的JWT>' \
  -H 'X-Mineru-Base: http://127.0.0.1:8010'
```

```python
tiers = requests.get(f"{API}/v1/tiers", headers=hdr()).json()["data"]
# [{"id":"speed","description":"速度优先…","raw_tier":"flash"}, ...]
```

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `data[].id` | string | 语义档位：`speed` / `balanced` / `quality` |
| `data[].description` | string | 档位说明 |
| `data[].raw_tier` | string | 当前引擎下的原始档位（本地 flash/standard/advanced；云端 pipeline/vlm） |

| tier | 本地 raw | 云端 raw | 含义 |
| --- | --- | --- | --- |
| `speed` | flash | pipeline | 速度优先：最快出结果，适合预览与批量草稿 |
| `balanced` | standard | pipeline | 均衡：质量与速度折中，推荐默认 |
| `quality` | advanced | vlm | 效果最佳：解析最准确，速度较慢 |

<a id="upload"></a>

## 文件上传（三步协议）

与 OpenAI Files 一致的分步协议：**声明元信息 → 传原始字节 → 提交完成**。三步缺一不可。

### 第 1 步 · 创建会话（POST /v1/uploads）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `filename` | string | 是 | 文件名（含扩展名，用于类型识别） |
| `bytes` | integer | 是 | **文件精确字节数（>0）**，第 2 步按此校验；上限 200MB |
| `mime_type` | string | 否 | 如 `application/pdf`，默认 `application/octet-stream` |

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/mineru/v1/uploads \
  -H 'Authorization: Bearer <你的JWT>' \
  -H 'X-Mineru-Base: http://127.0.0.1:8010' \
  -H 'Content-Type: application/json' \
  -d '{"filename":"demo.pdf","bytes":123456,"mime_type":"application/pdf"}'
```

```python
import os
FILE = "demo.pdf"
r = requests.post(f"{API}/v1/uploads", headers=hdr(), json={
    "filename": os.path.basename(FILE),
    "bytes": os.path.getsize(FILE),          # 必须是精确字节数
    "mime_type": "application/pdf",
})
upload_id = r.json()["id"]
```

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 上传会话 ID（`upload_*`），第 2 / 3 步引用 |

### 第 2 步 · 传字节（PUT /v1/uploads/{upload_id}/content）

- 请求体为**原始二进制**（curl 用 `--data-binary @file`），不是 multipart/form-data
- `Content-Type` 建议与第 1 步 `mime_type` 一致
- 错误：`404` 会话不存在 · `409` 会话已完成/已取消 · `413` 字节数与 `bytes` 不符或超上限

```bash
curl -s -X PUT http://127.0.0.1:8000/api/v1/mineru/v1/uploads/upload_xxx/content \
  -H 'Authorization: Bearer <你的JWT>' \
  -H 'X-Mineru-Base: http://127.0.0.1:8010' \
  -H 'Content-Type: application/pdf' \
  --data-binary @demo.pdf
```

```python
with open(FILE, "rb") as f:
    r = requests.put(f"{API}/v1/uploads/{upload_id}/content",
                     headers=hdr({"Content-Type": "application/pdf"}), data=f)
r.raise_for_status()
```

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ok` | bool | 恒为 `true`，表示字节已接收并通过字节数校验 |

### 第 3 步 · 完成（POST /v1/uploads/{upload_id}/complete）

无请求体（可传 `{}`）。完成后会话不可再 PUT。

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/mineru/v1/uploads/upload_xxx/complete \
  -H 'Authorization: Bearer <你的JWT>' \
  -H 'X-Mineru-Base: http://127.0.0.1:8010'
```

```python
r = requests.post(f"{API}/v1/uploads/{upload_id}/complete", headers=hdr())
file_id = r.json()["file"]["id"]
```

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 上传会话 ID |
| `file.id` | string | **产物文件 ID（`file_*`），即创建解析任务时引用的 `file_id`** |
| `file.filename` | string | 文件名 |

?> 上传会话默认 1 小时过期（本地引擎）；`bytes` 不准是最常见的 413 原因。云端任务注册表在 kb-api 重启后清空。

<a id="jobs"></a>

## 解析任务

### 创建任务（POST /v1/parse/jobs）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `files` | array | 是 | 1–100 个 `{source, page_range?}`；多文件合并为一个任务 |
| `files[].source` | object | 是 | 统一为 `{"type":"file_id","file_id":"file-..."}`（三步上传产物） |
| `files[].page_range` | string | 否 | 页码选择，见 [page_range 语法](#pagerange)，仅对 PDF 生效 |
| `ocr_mode` | string | 否 | `auto`（默认）/ `txt` 仅内嵌文字层 / `ocr` 强制 OCR（扫描件） |
| `output_formats` | array | 否 | 默认 `["markdown","structured_json"]` |
| `tier` | string | 否 | 语义三级或引擎原始档位，默认 `balanced`；未知值回退 balanced |

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/mineru/v1/parse/jobs \
  -H 'Authorization: Bearer <你的JWT>' \
  -H 'X-Mineru-Base: http://127.0.0.1:8010' \
  -H 'Content-Type: application/json' \
  -d '{"files":[{"source":{"type":"file_id","file_id":"file-yyy"}}],
       "ocr_mode":"auto","output_formats":["markdown","structured_json"],"tier":"balanced"}'
```

```python
r = requests.post(f"{API}/v1/parse/jobs", headers=hdr({"Content-Type": "application/json"}), json={
    "files": [{"source": {"type": "file_id", "file_id": file_id}}],
    "ocr_mode": "auto",
    "output_formats": ["markdown", "structured_json"],
    "tier": "balanced",
})
r.raise_for_status()
job_id = r.json()["job_id"]
```

响应字段（任务对象 JobOut，轮询接口返回同构）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `job_id` | string | 任务 ID（`job_*`），轮询 / 取消 / 下载 zip 时引用 |
| `status` | string | `queued` / `running` / `completed` / `partial` / `failed` / `canceled` |
| `created_at` | string | 创建时间（ISO 8601） |
| `tier` | string | 语义档位（speed / balanced / quality） |
| `raw_tier` | string | 引擎原始档位（如 flash / pipeline），便于排查 |
| `progress` | object | `{completed, failed, total}`；已处理数 = completed + failed |
| `files[].name` | string | 文件名 |
| `files[].output_files.markdown` | object | `{file_id, bytes}`，markdown 产物引用 |
| `files[].output_files.structured_json` | object | `{file_id, bytes}`，结构化 JSON 产物引用 |
| `files[].error.message` | string | 单文件失败原因（失败时） |

响应示例：

```json
{
  "job_id": "job_zzz", "status": "queued", "tier": "balanced", "raw_tier": "standard",
  "progress": {"completed": 0, "failed": 0, "total": 1},
  "files": [{"name": "demo.pdf", "output_files": null}]
}
```

### 任务列表（GET /v1/parse/jobs）

把历史任务合并进本地队列（仅列表级信息，详情需再查单个任务）。无请求参数。

```bash
curl -s http://127.0.0.1:8000/api/v1/mineru/v1/parse/jobs \
  -H 'Authorization: Bearer <你的JWT>' \
  -H 'X-Mineru-Base: http://127.0.0.1:8010'
```

```python
jobs = requests.get(f"{API}/v1/parse/jobs", headers=hdr()).json()["data"]
```

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `data[].job_id` | string | 任务 ID |
| `data[].status` | string | 任务状态（同上） |
| `data[].created_at` | string | 创建时间（ISO 8601） |
| `data[].file_count` | int | 任务内文件数 |

### 轮询状态 / 进度 / 产物（GET /v1/parse/jobs/{job_id}）

- 建议 **2 秒**间隔轮询；`status` 进入 `completed / partial / failed / canceled` 即停止
- 进度按 `progress:{completed,failed,total}` 计算：已处理数 = completed + failed
- 每个文件的结果在 `files[].output_files`，凭其中 `file_id` 去下载产物（字段说明见[创建任务响应](#jobs)）

```bash
curl -s http://127.0.0.1:8000/api/v1/mineru/v1/parse/jobs/job_zzz \
  -H 'Authorization: Bearer <你的JWT>' \
  -H 'X-Mineru-Base: http://127.0.0.1:8010'
```

```python
import time
while True:
    job = requests.get(f"{API}/v1/parse/jobs/{job_id}", headers=hdr()).json()
    print("status:", job["status"])
    if job["status"] in ("completed", "partial", "failed", "canceled"):
        break
    time.sleep(2)
```

完成时的响应示例：

```json
{
  "job_id": "job_zzz", "status": "completed", "tier": "balanced", "raw_tier": "standard",
  "progress": {"completed": 1, "failed": 0, "total": 1},
  "files": [{"name": "demo.pdf", "output_files": {
    "markdown": {"file_id": "file-mdout", "bytes": 8123},
    "structured_json": {"file_id": "file-jsout", "bytes": 45231}
  }}]
}
```

### 取消任务（DELETE /v1/parse/jobs/{job_id}）

仅 `queued / running` 可取消。**云端 SaaS 不支持取消（返回 409）**，调用前可查 `capabilities.cancelable`。

```bash
curl -s -X DELETE http://127.0.0.1:8000/api/v1/mineru/v1/parse/jobs/job_zzz \
  -H 'Authorization: Bearer <你的JWT>' \
  -H 'X-Mineru-Base: http://127.0.0.1:8010'
```

```python
r = requests.delete(f"{API}/v1/parse/jobs/{job_id}", headers=hdr())   # 云端 → 409
```

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ok` | bool | 恒为 `true`，表示取消成功 |

<a id="files"></a>

## 产物下载

### 下载产物内容（GET /v1/files/{file_id}/content）

直接返回**文件原文**（不是 JSON 包装）：`file_id` 来自任务详情的 `output_files.*.file_id`。

```bash
# 下载 markdown 产物
curl -s http://127.0.0.1:8000/api/v1/mineru/v1/files/file-mdout/content \
  -H 'Authorization: Bearer <你的JWT>' \
  -H 'X-Mineru-Base: http://127.0.0.1:8010' \
  -o result.md
# 下载结构化 JSON 产物
curl -s http://127.0.0.1:8000/api/v1/mineru/v1/files/file-jsout/content \
  -H 'Authorization: Bearer <你的JWT>' \
  -H 'X-Mineru-Base: http://127.0.0.1:8010' \
  -o structured.json
```

```python
md = requests.get(f"{API}/v1/files/{md_id}/content", headers=hdr()).content   # bytes，自行写文件
sj = requests.get(f"{API}/v1/files/{sj_id}/content", headers=hdr()).json()    # 结构化 JSON
```

响应说明：

| 响应 | Content-Type | 说明 |
| --- | --- | --- |
| markdown 产物 | `text/markdown` | Markdown 原文（本地引擎图片 base64 内联） |
| structured_json 产物 | `application/json` | 结构化解析结果（本地 middle.json / 云端 content_list.json） |

### 下载原始结果 zip（GET /v1/parse/jobs/{job_id}/files/{file_index}/result-zip，仅云端）

云端引擎的图片打包在 zip 内（`full.md` + `images/` + `content_list.json`）；`file_index` 为任务内文件序号（0 起）。**本地引擎返回 404**（markdown base64 自包含，直接下载 `.md` 即可）。

```bash
curl -s http://127.0.0.1:8000/api/v1/mineru/v1/parse/jobs/job_zzz/files/0/result-zip \
  -H 'Authorization: Bearer <你的JWT>' \
  -H 'X-Mineru-Base: cloud' \
  -o demo.zip
```

```python
r = requests.get(f"{API}/v1/parse/jobs/{job_id}/files/0/result-zip", headers=hdr())
if r.status_code == 200:
    open("demo.zip", "wb").write(r.content)
```

响应说明：

| 响应 | Content-Type | 说明 |
| --- | --- | --- |
| 成功 | `application/zip` | zip 内含 `full.md`、`images/` 图片目录、`content_list.json` |
| 本地引擎 | — | 404，改用 markdown 产物接口 |

?> 仅下载 markdown 文本时，云端产物内的相对路径图片不可见；需要完整图文请下载 zip。

<a id="states"></a>

## 状态机速查

| 对象 | 状态流转 |
| --- | --- |
| 解析任务 Job | `queued` → `running` → `completed` / `partial`（部分文件失败）/ `failed` / `canceled` |
| 任务内文件 | `queued` → `running` → `completed` / `failed` |

<a id="pagerange"></a>

## page_range 语法（PDF 页码选择）

- 1 起始、闭区间，逗号分隔多段：`"1-5,8"`
- `r1` = 最后一页、`r3-r1` = 倒数第 3 到最后一页；`"all"` = 全部页
- 选择会自动**排序去重**，越界页忽略；空白 = 未指定（全部）
- 非法：倒序区间（`5-1`）、`~`、负数页码 → 400

<a id="e2e"></a>

## 端到端示例（curl / Python）

同一「登录 → 上传 → 建任务 → 轮询 → 下载产物」流程的两种语言实现，任选其一。把 `KB`、引擎地址、账号换成实际值。

### curl 版（可直接保存为 parse.sh）

```bash
#!/usr/bin/env bash
set -e
KB=http://127.0.0.1:8000
XB='X-Mineru-Base: http://127.0.0.1:8010'    # 云端 SaaS 改为 'X-Mineru-Base: cloud'
FILE=$1; NAME=$(basename "$FILE"); SIZE=$(stat -f%z "$FILE")
MIME=$(file --brief --mime-type "$FILE")
API=$KB/api/v1/mineru

# 0) 登录换 JWT
TOKEN=$(curl -s -X POST $KB/api/v1/auth/login -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"admin123\"}" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')
AUTH="Authorization: Bearer $TOKEN"

# 1) 会话
UP=$(curl -s -X POST $API/v1/uploads -H "$AUTH" -H "$XB" -H 'Content-Type: application/json' \
  -d "{\"filename\":\"$NAME\",\"bytes\":$SIZE,\"mime_type\":\"$MIME\"}")
UID_=$(echo "$UP" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')

# 2) 字节
curl -s -X PUT "$API/v1/uploads/$UID_/content" -H "$AUTH" -H "$XB" \
  -H "Content-Type: $MIME" --data-binary @"$FILE" >/dev/null

# 3) 完成 → file_id
FID=$(curl -s -X POST "$API/v1/uploads/$UID_/complete" -H "$AUTH" -H "$XB" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["file"]["id"])')

# 4) 建任务
JOB=$(curl -s -X POST $API/v1/parse/jobs -H "$AUTH" -H "$XB" -H 'Content-Type: application/json' \
  -d "{\"files\":[{\"source\":{\"type\":\"file_id\",\"file_id\":\"$FID\"}}],\"tier\":\"balanced\"}")
JID=$(echo "$JOB" | python3 -c 'import json,sys;print(json.load(sys.stdin)["job_id"])')

# 5) 轮询
while :; do
  ST=$(curl -s $API/v1/parse/jobs/$JID -H "$AUTH" -H "$XB" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])')
  echo "status: $ST"; case $ST in completed|partial|failed|canceled) break;; esac; sleep 2
done

# 6) 下载 markdown 产物
MDID=$(curl -s $API/v1/parse/jobs/$JID -H "$AUTH" -H "$XB" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["files"][0]["output_files"]["markdown"]["file_id"])')
curl -s $API/v1/files/$MDID/content -H "$AUTH" -H "$XB" -o "${NAME%.*}.md"
echo "done → ${NAME%.*}.md"
```

### Python 版（可直接保存为 parse.py，仅依赖 requests）

```python
#!/usr/bin/env python3
# 依赖：pip install requests    用法：python parse.py demo.pdf
import os, sys, time
import requests

KB = "http://127.0.0.1:8000"                    # kb-api 地址
ENGINE = "http://127.0.0.1:8010"                # 云端 SaaS 改为 "cloud"
FILE = sys.argv[1] if len(sys.argv) > 1 else "demo.pdf"
MIME = "application/pdf"                        # 按文件实际 MIME 调整
API = f"{KB}/api/v1/mineru"

# 0) 登录换 JWT
token = requests.post(f"{KB}/api/v1/auth/login",
                      json={"username": "admin", "password": "admin123"}).json()["access_token"]

def hdr(extra=None):
    h = {"Authorization": f"Bearer {token}", "X-Mineru-Base": ENGINE}
    h.update(extra or {})
    return h

# 1) 创建上传会话（只声明元信息，不传内容）
r = requests.post(f"{API}/v1/uploads", headers=hdr(), json={
    "filename": os.path.basename(FILE),
    "bytes": os.path.getsize(FILE),             # 必须是精确字节数
    "mime_type": MIME,
})
r.raise_for_status()
upload_id = r.json()["id"]

# 2) PUT 原始字节
with open(FILE, "rb") as f:
    r = requests.put(f"{API}/v1/uploads/{upload_id}/content",
                     headers=hdr({"Content-Type": MIME}), data=f)
r.raise_for_status()

# 3) 完成上传 → file_id
r = requests.post(f"{API}/v1/uploads/{upload_id}/complete", headers=hdr())
r.raise_for_status()
file_id = r.json()["file"]["id"]

# 4) 创建解析任务
r = requests.post(f"{API}/v1/parse/jobs", headers=hdr({"Content-Type": "application/json"}), json={
    "files": [{"source": {"type": "file_id", "file_id": file_id}}],
    "ocr_mode": "auto",
    "output_formats": ["markdown", "structured_json"],
    "tier": "balanced",
})
r.raise_for_status()
job_id = r.json()["job_id"]

# 5) 轮询任务直至终态（建议 2 秒间隔）
while True:
    job = requests.get(f"{API}/v1/parse/jobs/{job_id}", headers=hdr()).json()
    print("status:", job["status"])
    if job["status"] in ("completed", "partial", "failed", "canceled"):
        break
    time.sleep(2)

# 6) 下载 markdown 产物（文件原文，非 JSON 包装）
md_id = job["files"][0]["output_files"]["markdown"]["file_id"]
out = os.path.splitext(FILE)[0] + ".md"
with open(out, "wb") as f:
    f.write(requests.get(f"{API}/v1/files/{md_id}/content", headers=hdr()).content)
print("done →", out)
```

<a id="frontend"></a>

## 前端集成（本项目）

浏览器统一走本契约（kb-api 同源 `/api/v1/mineru/*`），JWT 由 axios 拦截器自动附带；引擎由 `X-Mineru-Base` 请求头指定，代理需 **admin / super_admin** JWT。

```javascript
// 例：创建解析任务（axios 实例已带 Authorization: Bearer <jwt>）
await request({
  url: '/mineru/v1/parse/jobs',          // 实际命中 kb-api /api/v1/mineru/v1/parse/jobs
  method: 'POST',
  headers: { 'X-Mineru-Base': 'http://127.0.0.1:8010' },   // 云端 SaaS 传 'cloud'
  data: {
    files: [{ source: { type: 'file_id', file_id: 'file-...' } }],
    ocr_mode: 'auto',
    output_formats: ['markdown', 'structured_json'],
    tier: 'balanced',
  },
})
```

- 代理路径：`/api/v1/mineru/{契约路径}`，方法 / 查询串 / 请求体原样透传
- 大文件上传 / 产物下载代理超时 300s；前端参考实现：`ParserCustomTab.vue → api() / uploadOne()`
- axios 对字符串 body 不会自动设 Content-Type，**必须显式加 `application/json`**，否则解析会 400
- 显式路由未覆盖的本地 kit 私有端点由 catch-all 兜底透传（云端模式一律 404）

## 常见错误排查

| 现象 | 原因与处理 |
| --- | --- |
| `401` | 未带 / JWT 过期 → 按[鉴权与获取 Token](#auth) 重新登录换 token |
| `403` | 角色不足：契约端点仅 admin / super_admin 可用 |
| `400` | JSON 请求未带 `Content-Type: application/json`；或 page_range 语法非法 |
| `404` | upload_id / job_id / file_id 拼写错误；上传会话已过期；云端模式调用了仅本地支持的端点 |
| `409` | 会话已完成后再 PUT；对终态任务取消；**云端 SaaS 不支持取消任务** |
| `413` | 声明 `bytes` 与实际字节数不一致；或文件超过 200MB 上限 |
| `502` | 本地引擎连接失败 → 检查 `X-Mineru-Base` 指向的服务是否存活 |
| `503` | 云端模式未配置 MinerU API Key → 模型配置页填写并保存 |
| 轮询中断 | 网络抖动连续失败应熔断并提示「从服务端拉取」恢复，避免错误刷屏 |

---

解析引擎 API 调用文档 · 对应 kb-api 统一契约（`services/kb-api/app/routes/mineru_route.py` + `mineru_contract.py`）· 本地引擎 http://127.0.0.1:8010 · 产物格式固定 markdown + structured_json
