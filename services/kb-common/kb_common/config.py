from functools import lru_cache
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 仓库根目录（config.py 在 services/kb-common/kb_common/config.py，上三级为仓库根）下的 .env
# 无论从哪个服务目录运行都能找到
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ENV_FILE = _REPO_ROOT / ".env"

class Settings(BaseSettings):
    # Enterprise QA: CLI credentials stay with DWS; mapping binds web users to DWS identities.
    qa_dws_user_profiles: str = "{}"
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")

    # 基础设施
    database_url: str = "postgresql+asyncpg://dev:dev123456@127.0.0.1:5432/dev_db"
    # 连接池：SQLAlchemy 默认 pool_size=5 / max_overflow=10 / pool_timeout=30，
    # 前端单页并发 7-12 个请求（每个请求鉴权即占用一条连接）时极易排队到 30s 才报错。
    # 这里把常驻热连接放大到 15、等待上限压到 10s：
    # 实测本机 Docker VM 会偶发把「新建一条 PG 连接」拖到 6~39s（常态仅 22~40ms），
    # 而 pool_size 内的连接建好后常驻不回收，因此让常驻连接覆盖单用户峰值并发
    # （约 11~13 条）就几乎不会触发新建；溢出部分相应收小，避免多进程叠加后
    # 顶穿 PostgreSQL 默认 max_connections=100。宁可快速失败，也不要让界面卡几十秒。
    db_pool_size: int = Field(default=15, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    db_pool_timeout: int = Field(default=10, ge=1)
    db_pool_recycle: int = Field(default=1800, ge=-1)
    redis_url: str = "redis://:dev123456@127.0.0.1:6379/0"
    es_host: str = "http://127.0.0.1:9200"
    minio_endpoint: str = "127.0.0.1:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    kkfv_url: str = "http://127.0.0.1:8012"

    # 认证
    jwt_secret: str = "change-me-in-prod"
    jwt_algo: str = "HS256"
    jwt_ttl_minutes: int = 1440

    # 模型（全部外接，不本地部署 BGE-M3/reranker——启动不拉起小模型）
    # 向量模型：OpenAI 兼容 /embeddings 端点；留空时本地 RAG 检索/入库不可用（Dify/RAGFlow 检索不受影响）
    embedding_base_url: str = ""     # 如 https://api.siliconflow.cn/v1（需含 /v1，末尾不带 /embeddings）
    embedding_api_key: str = ""
    embedding_model: str = ""        # 如 BAAI/bge-m3（API 版）；维度须与既有 ES 索引一致（embed_dim）
    embed_dim: int = 1024
    # 重排模型：Jina/SiliconFlow 风格 /rerank 端点；留空时检索跳过重排（RRF/BM25 排序兜底）
    rerank_api_url: str = ""         # 如 https://api.siliconflow.cn/v1/rerank
    rerank_api_key: str = ""
    rerank_model: str = ""           # 如 BAAI/bge-reranker-v2-m3
    mineru_api_url: str = "https://mineru.net/api/v4"
    mineru_api_key: str = ""          # 运行时可被 settings 表覆盖
    # 本地 MinerU 解析引擎（mineru-api 常驻服务，见 services/mineru）；
    # 置空字符串可禁用本地引擎、强制走云 API。
    mineru_local_url: str = "http://127.0.0.1:2028"
    # mineru-kit V1 API（解析引擎本地通道，可取 structured_content 结构化 JSON）；
    # 置空字符串则页内仅可选 MinerU 云 API 通道。
    structured_kit_base_url: str = "http://127.0.0.1:8010"

    # LLM（默认 GLM；DeepSeek 同协议）
    llm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    llm_api_key: str = ""
    llm_model: str = "glm-4-flash"

    # Dify 知识库（用于智能问答 Agent；Service API 端点需含端口与 /v1）
    dify_base_url: str = "http://127.0.0.1:8088/v1"
    dify_api_key: str = ""           # Dataset API Key 或 App API Key
    dify_dataset_ids: str = ""       # 默认检索的数据集 ID，逗号分隔
    # 可选：Dify 自身数据库连接串（如 postgresql+psycopg://postgres:xxx@127.0.0.1:5432/dify）。
    # Dify Service API 不暴露流水线的 input form 变量定义（分段参数 schema），
    # 配置后 kb-api 可直连只读查询 workflows.rag_pipeline_variables，
    # 前端据此自动生成「流水线分段参数」配置表单；留空则前端降级为 JSON 编辑器。
    dify_db_url: str = ""
    # Dify 的 ETL 类型（与 Dify 侧 ETL_TYPE 环境变量保持一致）：dify | Unstructured。
    # Service API 不暴露该配置，故在此镜像一份：决定同步/预演使用的文档扩展名白名单
    # （内置 ETL 不支持 pptx/doc/eml 等；Unstructured 支持但需 Dify 配 UNSTRUCTURED_API_URL）。
    dify_upload_max_mb: int = Field(default=15, ge=1, le=1024)
    dify_etl_type: str = "dify"
    dify_retrieval_top_k: int = 50
    dify_score_threshold: float = 0.4
    # 检索方式：semantic_search / full_text_search / hybrid_search
    dify_search_method: str = "semantic_search"
    # Dify 侧重排（需在 Dify 控制台配置 Rerank 模型；未配置时应置 false，否则检索 400）
    dify_reranking_enable: bool = False

    # RAGFlow 知识库（第二个外部检索引擎，与 Dify 并列；Service API 端点需含 /api/v1）
    # 只维护「连接」——具体哪些库可用在「知识源管理」里登记（source_type=ragflow_dataset）。
    # api_key 留空即视为未配置：RAGFlow 相关接口 fail-fast 报错，不做假数据兜底。
    ragflow_base_url: str = "http://127.0.0.1:9380/api/v1"
    ragflow_api_key: str = ""            # 运行时可被 settings 表覆盖
    ragflow_retrieval_top_k: int = 8
    ragflow_similarity_threshold: float = 0.2
    # 向量相似度权重（0=纯关键词，1=纯向量）；配了 rerank 时该项被忽略
    ragflow_vector_similarity_weight: float = 0.3
    ragflow_rerank_id: str = ""          # 如 BAAI/bge-reranker-v2-m3；留空则不重排
    ragflow_parse_timeout_seconds: int = Field(default=600, ge=1)

    # 服务间
    kb_api_internal_url: str = "http://127.0.0.1:8000"

    # 钉钉（企业知识库数据来源）
    dingtalk_app_key: str = ""
    dingtalk_app_secret: str = ""
    dingtalk_operator_union_id: str = ""   # 调用知识库 API 的操作人 unionId
    dingtalk_robot_code: str = ""          # 企业内机器人编码（知识缺口通知发单聊消息）

    # 钉钉知识库 → Dify 定时增量同步（合并自 DingDingKonwledgePipeline）
    # dws CLI（导出 ALIDOC/.able 在线文档所需）
    dws_bin: str = "dws"
    dws_config_dir: str = ""   # 空则启动时回退到 services/kb-api/data/.dws
    # 同步引擎参数
    sync_local_storage_dir: str = ""   # 空则回退到 services/kb-api/data/sync_downloads
    sync_max_depth: int = 10
    sync_max_results_per_page: int = 50
    sync_export_format: str = "markdown"   # markdown | docx | pdf
    sync_skip_extensions: str = ".jpg,.jpeg,.png,.gif,.mp4,.mp3,.zip,.rar,.7z"
    sync_dify_wait_indexing: bool = True
    # 监督进程硬截止：超时强杀进程，已完成部分保留为 partial。
    # 默认 7 天（604800s）——大库/慢解析（RAGFlow DeepDoc/OCR）不应被分钟级上限截断。
    sync_run_timeout_seconds: int = Field(default=604800, ge=1)
    sync_export_timeout_seconds: int = Field(default=360, ge=1)
    # 单文档解析/索引等待上限：默认 7 天，避免 RAGFlow/Dify 长解析被 600s 截断。
    sync_indexing_timeout_seconds: int = Field(default=604800, ge=1)
    sync_default_delete_policy: str = "keep"   # keep | sync

    # 定时同步调度：XXL-Job 调度中心（替代内置 APScheduler）。
    # enabled=true 时 admin 不可达启动即报错（fail fast）；显式置 false 则定时同步整体关闭。
    xxl_job_enabled: bool = True
    xxl_job_admin_url: str = "http://127.0.0.1:8080/xxl-job-admin"
    xxl_job_admin_user: str = "admin"
    xxl_job_admin_password: str = "123456"
    # 与 admin 的 xxl.job.accessToken 保持一致；留空表示不启用 token 校验。
    xxl_job_access_token: str = ""
    xxl_job_executor_appname: str = "kge-sync-executor"
    xxl_job_executor_port: int = 9998
    xxl_job_executor_ip: str = ""   # 空则按到 admin 的出口 IP 自动探测
    xxl_job_executor_log_path: str = ""   # 空则回退到 services/kb-api/data/xxljob-log

    @property
    def sync_skip_ext_list(self) -> list[str]:
        return [e.strip().lower() for e in self.sync_skip_extensions.split(",") if e.strip()]

@lru_cache
def get_settings() -> Settings:
    return Settings()
