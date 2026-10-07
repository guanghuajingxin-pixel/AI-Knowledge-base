# 知识库检索

统一检索已开放的本地文档库、Dify 和 RAGFlow，返回知识片段及来源。

## 请求

```http
POST /api/openapi/v1/knowledge/retrieve
X-API-Key: <完整平台 API Key>
Content-Type: application/json
```

### 请求字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| query | string | 是 | 非空检索词，最多 2000 字符 |
| library_ids | integer[] | 是 | 1–24 个知识库 ID，使用平台知识库 ID，而非底层 dataset ID |
| top_k | integer | 否 | 合并后最多返回条数，默认 8，范围 1–50 |
| mode | string | 否 | hybrid 混合、vector 向量、fulltext 全文；省略遵循库级设置或引擎默认 |
| similarity_threshold | number | 否 | 最终分数阈值，0–1 |
| vector_similarity_weight | number | 否 | 混合检索向量权重，0–1 |
| rerank | boolean | 否 | 是否重排；省略遵循库级设置或引擎默认 |
| rerank_model_id | string | 否 | 平台重排模型配置 UUID |
| rerank_id | string | 否 | RAGFlow 混合检索的引擎重排模型名，最多 200 字符 |
| document_ids | string[] | 否 | 最多 100 个文档 ID；本地库使用 UUID，RAGFlow 使用引擎文档 ID；Dify 暂不支持 |

```json
{
  "query": "设备维修流程",
  "library_ids": [1, 2],
  "top_k": 8,
  "mode": "hybrid",
  "similarity_threshold": 0.2,
  "vector_similarity_weight": 0.7
}
```

::: info 访问范围
拥有检索权限的应用可检索所有 enabled 的非素材库，当前不支持逐应用知识库 ACL。任一指定库不存在、停用或为素材库，整次请求返回 403。空列表不会触发全库检索。
:::

## 响应

```json
{
  "hits": [
    {
      "library_id": 1,
      "library_name": "维修知识库",
      "document_id": "65a2e86b-274e-4cc4-a9aa-ae155c0bf820",
      "document_title": "设备维修手册",
      "content": "先确认设备状态，再执行维修流程……",
      "score": 0.85
    }
  ],
  "total": 1,
  "elapsed_ms": 120,
  "partial": false,
  "failed_library_ids": []
}
```

| 字段 | 说明 |
| --- | --- |
| hits | 按分数排序的命中片段，包含库、文档、内容、分数 |
| total | 本次实际返回的片段数，不是库内全部潜在匹配数 |
| elapsed_ms | 检索耗时（毫秒），不包含完整鉴权和响应耗时 |
| partial | 是否有部分知识库检索失败 |
| failed_library_ids | 失败库 ID；不包含内部异常或连接配置 |

内容、文档标题和知识库名称按 viewer 的检索输出策略脱敏；策略服务异常时接口拒绝返回原文。分数由各引擎计算，不应视为统一校准的概率。

## Python 示例

```python
import os
import requests

response = requests.post(
    os.environ['KNOWLEDGE_BASE_URL'].rstrip('/') + '/api/openapi/v1/knowledge/retrieve',
    headers={'X-API-Key': os.environ['PLATFORM_API_KEY']},
    json={'query': '设备维修流程', 'library_ids': [1], 'top_k': 8},
    timeout=60,
)
response.raise_for_status()
result = response.json()
if result['partial']:
    print('部分库未完成检索：', result['failed_library_ids'])
for hit in result['hits']:
    print(hit['document_title'], hit['content'])
```

## 错误与降级

错误通常为 `{"detail":"原因"}`；422 参数校验的 detail 可能为数组。

| 状态码 | 含义 | 处理建议 |
| --- | --- | --- |
| 200 | 成功；无命中时 hits 为空 | 检查 partial，必要时补查失败库 |
| 401 | 凭证错误、已停用或已撤销 | 检查完整 Key 与启用状态 |
| 403 | 缺少权限，或指定库未开放 | 检查角色与库状态 |
| 422 | 参数校验失败 | 按字段约束修正请求 |
| 502 | 所有目标库检索失败 | 检查检索引擎和模型连接 |
| 503 | 统一鉴权或脱敏服务不可用 | 恢复依赖后重试 |

部分库失败仍返回 200 和成功库结果；本地向量服务不可用时沿用全文降级，重排不可用时保留召回排序。请求重试应设置退避，避免故障时持续密集调用。
