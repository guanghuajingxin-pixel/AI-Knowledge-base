# 物料库（一期）

## 范围与入口

知识库新增独立「物料库」页签，注册表 `library_type=material / platform=material`。不进入文本问答的 Dify/RAGFlow 路由，也不把图片转换成文本分段。

- 库：新建、修改设置、启停、清空后删除。
- 物料：库内唯一料号、名称、规格、分类、标签、说明、启停；每个物料最多 20 张照片。
- 图片：JPG/PNG/静态 WebP，20 MB、2000 万像素上限；可关联物料或暂不关联；上传时可框选主体、可选抠图，异步向量化。
- 预览：原图（显示时剥离 EXIF）、透明前景、向量化标准图；图片启停、绑定/解绑物料、失败重试、删除。
- 检索：上传查询图、框选主体、分类精确过滤和规格包含过滤；按物料聚合候选、对比图片、查看该物料的其他照片。
- 模型组件：在「系统配置 → 图片组件」或「物料库 → 图片组件」维护；仅管理员可写与测试组件，管理员/编辑者可管理物料库，已登录用户按现有共享工作区模型读取和检索。没有引入新的细粒度 ACL。

## 部署

```bash
cd services/kb-api
uv sync
cd ../kb-common
uv run alembic -c ../../alembic.ini heads
uv run alembic -c ../../alembic.ini upgrade head
cd ../kb-api
uv run celery -A app.worker worker -Q ingestion --concurrency=2 -l info
```

部署已有 Celery worker 必须滚动重启以注册 `process_material_image`；API 同样部署新版本。图片任务使用现有 Redis ingestion 队列。新增依赖只有 Pillow，不在业务进程加载模型。

新迁移 `0049_material_libraries` 创建 `image_components / materials / material_images`，图片模型位于 `kb_common/material_models.py`，在 Alembic env 中显式注册。原文件与图片产物存入现有 MinIO，向量存入现有 Elasticsearch 8.x。

## 阿里云百炼配置

新增图像向量组件：

| 参数 | 初始建议 |
| --- | --- |
| 协议 | 阿里云百炼 |
| 模型 | `multimodal-embedding-v1` |
| 维度 | `1024`（此模型固定维度） |
| 地址 | `https://dashscope.aliyuncs.com/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding` |
| API Key | 用户在配置界面填写；GET 不回显，更新凭据单独操作 |

该适配器发送 `input.contents=[{"image":"data:image/jpeg;base64,..."}]`，读取 `output.embeddings[0].embedding`；按官方模型规则决定是否发送 `parameters.dimension`。支持的模型/维度在 `ComponentIn` 校验，其他区域需选择该区域支持的模型和对应官方域名。不是文本模型 `/compatible-mode/v1/embeddings` 接口。

可改为 `qwen3-vl-embedding` 等已支持的多模态模型，但不能只修改旧组件的模型名或维度。需新增一个组件版本、修改库默认，再对需要迁移的图片选择「采用当前库组件」重新处理。

官方接口依据：<https://help.aliyun.com/zh/model-studio/multimodal-embedding-api-reference>

## 抠图 HTTP 协议

SaaS 选型、阿里云开通步骤、自建方案及 VL 能力边界见 [抠图接入方案与配置指南](material-background-removal-guide.md)。当前自定义 HTTP 需适配服务，不能直接填写云厂商原生 API 地址。

配置用途「图片抠图」、协议「自定义 HTTP」，填写完整接口地址与可选 Bearer API Key。推荐后端部署预训练 BiRefNet，但本项目只消费 HTTP，不安装/训练模型。

请求：`POST <endpoint>`，`multipart/form-data`：

- `file`：经过 EXIF 校正及用户框选后的 PNG 文件。
- `model`：组件中的模型名称（例如 BiRefNet）。

响应任选一种：

1. `Content-Type: image/png`，同尺寸、含 alpha 的透明 PNG。
2. `Content-Type: application/json`，`{"image_base64":"<PNG base64 或 data URI>"}`。

必须保留主体的真实结构，不能补画/生成零件细节；尺寸改变、全透明、全不透明、主体面积异常均拒绝。质量规则只能发现明显异常，需要用真实照片测试金属反光、孔洞及开口的效果。不下载服务返回的任意远端 URL。

未配置抠图组件时可以建立物料库，但开启预加工的上传必须先配置；用户也可明确关闭本次预加工。失败不会静默回退到原图并声称抠图成功。

## 自定义图像向量 HTTP 协议

为内网 DINOv2 等推理服务预留的适配边界，并不宣称所有 OpenAI 文本兼容接口支持此结构。

```json
{
  "model": "your-image-model-version",
  "input": [{"image": "data:image/jpeg;base64,..."}],
  "dimensions": 768
}
```

响应：`{"data":[{"index":0,"embedding":[...]}]}`。一期每次只发送一张图，严格校验数量、维度、有限值和非零范数，并做 L2 归一化。

## 处理、索引与一致性

1. 原图入 MinIO，PG 写入图片记录，绑定不可变组件 ID 和处理版本。
2. 发布持久化 Celery 任务；队列不可用时保留原图、显示失败并允许重试。
3. worker 校验图片任务代次，通过 PG advisory lock 避免同图并发处理；状态为 `PENDING → PROCESSING → INDEXING → COMPLETED`，失败有限重试后 `FAILED`。
4. 可选抠图 → alpha 主体裁剪 → 等比缩放至长边 920 → 居中到 1024×1024、RGB(245,245,245) 画布 → JPEG。没有填洞、美化或生成式修复。
5. 保存透明前景和标准图。临时向量服务错误的自动重试可复用已保存标准图。
6. ES 索引 `material_image_v1_<embedding component UUID>`，向量条目 ID 为 `image_id:run_id`。完成写索引且可见后才标记完成。

配置更改只影响新图。已有图片按原模型和预处理版本检索；同库混合抠图/未抠图时，查询图分别处理、分别召回。每组先按物料去重，再做 RRF 排名融合，避免多照片物料重复占位。查询最多 8 个配置组、60000 张图片；超过时明确提示缩小范围或统一配置。

查询图片只保留在请求内存中，不写入 MinIO/数据库，不自动加入图库。为每组返回标准化预览。原始余弦分数只用于查看技术详情，候选不是同料概率，也不承诺可从单张照片推断实际尺寸或材质牌号。

检索用 PG 当前可检索的 `entry_id` 对 ES 做预过滤，再在模型调用结束后校验库、物料、图片启停以及任务代次。删除/停用/重建后旧 ES 数据不能越过校验。删除先停用并使旧任务失效，再清理 ES、MinIO 和记录；清理失败保留停用记录，可再次删除。库/物料删除要求先清空图片，避免批量不可见的破坏性清理。

人工重新处理会移除该图旧索引，处理完成后恢复检索；一期不提供全库无停机重建。组件地址/模型/维度不可原地编辑，API Key 可独立轮换。底层模型版本应由服务端固定，不能在同一组件地址后偷偷换模型。

## 验证

```bash
cd services/kb-api
uv run python -m unittest discover -s tests -p 'test_material_images.py'
MATERIAL_INTEGRATION=1 uv run python -m unittest discover -s tests -p 'test_material_integration.py'
MATERIAL_INTEGRATION=1 uv run python -m unittest discover -s tests -p 'test_material_worker.py'
uv run python -m unittest discover -s tests -p 'test_managed_libraries.py'
cd ../../web
pnpm build
```

单元测试覆盖抠图空蒙版/不透明结果、孔洞保留、等比标准化、EXIF、框选边界、文件限制、模型维度、非法向量、百炼请求结构、鉴权错误脱敏和多图去重。

集成测试使用实际 PG/MinIO/ES，以唯一命名的临时库和图片验证入库、任务幂等、索引检索、跨库隔离、料号去重、启停、删除清理；模型响应为模拟数据，不代表真实抠图/Embedding 质量。测试只清理它自身创建的数据。

worker 测试还会启动仅监听独立临时队列的 Celery worker 和本地模拟模型 HTTP 服务，验证 Redis 投递、真实异步抠图/向量化状态与 ES 命中；不会消费正常 ingestion 队列，完成后清理测试资源。

真实模型配置与效果验收由用户稍后进行。建议独立拍摄查询图，避免把已入库图片本身当作准确率测试集。

### 配置查看与真实连通性测试

在「系统配置 → 图片组件」（或物料库的「图片组件」）点击「查看配置」，可查看已保存的名称、用途、协议、接口地址、模型及维度；密钥仅显示配置状态，可单独更新。模型及维度仍按不可变组件版本管理。

详情中的「测试连通性」通过 `POST /api/v1/image-components/{id}/test-connection` 使用内置图片实际调用已保存的模型，校验鉴权、响应格式和向量维度；抠图组件同时校验输出透明图。该操作需要管理员权限，会产生少量模型调用费用。结果显示耗时、维度或可操作的错误提示。「测试图片」保留自选图片和主体框测试能力。

2026-09-29 真实百炼联调：`multimodal-embedding-v1` / 1024 维，附件金属零件照片经 API 上传、Celery 处理、MinIO 存储、ES 入库及同图检索成功；同图余弦相似度约 0.99977。测试样本保留在「物料图片实测库」。本次使用原图模式，未配置真实抠图服务；同图命中只验证链路，不代表跨角度物料识别准确率。修复真实服务 gzip 响应被重复解压的问题，并增加压缩响应回归测试。
