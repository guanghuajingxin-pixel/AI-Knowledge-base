# Knowledge Governance Expert（知识治理专家）

知识治理专家：文档采集 / 切片 / 向量检索 / LLM 问答 + FAQ 精准匹配 + **LangGraph 智能问答 Agent（统一知识库层：RAGFlow / DIFY 镜像，检索策略按库类型自动决定）**，3 进程微服务 + Docker 基础设施 + 本地 Dify 服务。

## 架构

> 完整的分层架构图、进程交互视图、关键链路时序图、数据架构与架构决策说明见 [docs/architecture.md](docs/architecture.md)（与代码实际实现同步维护）。

```
                     ┌─────────────────────────────────────────┐
   Web (Vue3)        │  本地原生进程（开发模式）                │
   :3000 (vite)      │                                         │
        │            │  kb-api:8000      FastAPI（模型外接）   │
        │  /api/v1   │   ├ 文档/目录/检索/问答/认证/设置        │
        │  /api/v1/  │   ├ DeerFlow Runner (SSE 转译/回退)     │
        │   faq      │   ├ 内置 LangGraph Agent (回退工作流)   │
        └─────┬─────►│   └ /internal/embed (转发外接向量 API)  │
              │      │                                         │
              │ HTTP │  deerflow:2027  DeerFlow 2.0 sidecar    │
              │  SSE │   (Python 3.12 + uv，独立 venv)         │
              ├─────►│   ├ Lead Agent (create_agent 中间件链)  │
              │      │   ├ Sub-Agent 并行调研 / Todo 规划      │
              │      │   ├ 长期记忆 + 摘要压缩 + sqlite 检查点 │
              │      │   └ knowledge_search 工具 → kb-api 检索 │
              │      │                                         │
                     │  faq-service:8004 FastAPI (FAQ KB)      │
                     │                                         │
                     │  kb-worker        Celery (ingestion)    │
                     │   ├ MinerU 解析                         │
                     │   └ chunk + embed + ES index            │
                     └────────────┬────────────────────────────┘
                                  │ (dev-network)
            ┌─────────────────────┼─────────────────────┐
            │   Docker 基础设施 (dev-services compose)  │
            │  dev-postgres:5432   dev-redis:6379       │
            │  dev-elasticsearch:9200  dev-minio:9000   │
            │  dev-kkfileview:8012                      │
            └─────────────────────┬─────────────────────┘
                                  │ HTTP (Dify Service API)
            ┌─────────────────────┴─────────────────────┐
            │  Dify (本地 dify/docker，docker compose)    │
            │  nginx:8088 → api:5001 / web:3000          │
            │  内置 weaviate / postgres / redis / sandbox │
            │  知识库检索端点：POST /datasets/{id}/retrieve│
            └───────────────────────────────────────────┘
```

**4 应用进程**（kb-api / faq-service / kb-worker / **deerflow sidecar**）+ **5 基础设施容器**（dev-services compose）+ **本地 Dify 服务**（`dify/docker`，独立 compose，默认 8088 端口）。

向量（BGE-M3 API 版）+ 重排（bge-reranker-v2-m3 API 版）**全部外接**（OpenAI 兼容 `/embeddings` 与 `/rerank` HTTP API，配置 `EMBEDDING_*` / `RERANK_*`），kb-api 不再本地加载模型；faq-service 与 kb-worker 经 `/internal/embed` 复用 kb-api 的外接通道。

**智能问答 Agent 底座：DeerFlow 2.0 sidecar**。问答能力由真实的 [DeerFlow 2.0 Enhanced](https://github.com/stophobia/deerflow2.0-enhanced) harness 驱动（vendor 于 [services/deerflow/backend](services/deerflow/backend)），而非自建流程图。DeerFlow harness 要求 Python ≥3.12（langchain 1.2.x / langgraph 1.0.x），与 kb-api 的 Python 3.11 + langchain 0.x 无法同环境，因此以**独立 sidecar 进程**运行（端口 2027）：

- **进程内嵌入式运行**：sidecar 通过 DeerFlow 官方 `DeerFlowClient`（`deerflow/client.py`）在进程内直接跑 Lead Agent（create_agent + 中间件链 + 子智能体 + 长期记忆 + 摘要压缩 + sqlite checkpointer），**无需** langgraph server / gateway 进程。入口 [services/deerflow/backend/app/qa_server.py](services/deerflow/backend/app/qa_server.py)（FastAPI + SSE）。
- **配置自举**：sidecar 启动时 `GET` kb-api `/api/v1/agent/bootstrap` 拉取模型配置（base_url/api_key/model/temperature），自动生成 `config.yaml` 与「杰克百晓生」人格 `SOUL.md`；kb-api 晚启动时后台重试（40 次 × 5s）。
- **企业知识接入（目录预判 + 双来源检索 + 钉钉正文读取）**：DeerFlow 通过自定义扩展工具 [extensions/kb_tools.py](services/deerflow/backend/extensions/kb_tools.py) 挂载四个工具（`config.yaml` 的 `use: 模块:变量` 动态导入），知识类问题**主动、同时检索两个来源**，无需征询：
  - `knowledge_search` → kb-api `POST /api/v1/internal/kb/retrieve`（头 `X-Internal-Token`，env `KB_INTERNAL_TOKEN`，默认 `kge-internal-dev-token`），落到**统一知识库层**（`knowledge_libraries` 登记的 RAGFlow / DIFY 镜像库，按 platform 分通道走各自引擎的语义+全文检索与重排策略），返回文档正文片段，是答案内容的主要来源；
  - `dingtalk_browse` → kb-api `POST /api/v1/internal/dingtalk/browse`，**目录浏览**（不读正文、速度快）：`action="map"` 返回有权限知识库 → 二级目录的地图（含各目录文档/在线文档计数），用于**先预判问题最可能落在哪个知识库与目录**；`action="list"` 按目录关键词模糊匹配返回该目录下的文档（node_id/标题/扩展名/online 标记，**在线文档排在最前**）；
  - `dingtalk_search` → kb-api `POST /api/v1/internal/dingtalk/search`，基于钉钉知识库文件缓存（文件名/目录路径关键词匹配，见上文「钉钉知识」），用于目录预判不到时补漏，发现知识库未收录的文档；
  - `dingtalk_read_doc` → kb-api `POST /api/v1/internal/dingtalk/content`，通过 `dws` CLI 读取钉钉文档**正文**（在线文档 `dws doc read` 直读 Markdown；二进制文件 `dws drive download` 下载后**优先经本地 MinerU 引擎解析为 Markdown**——ppt/pptx/doc/docx/xls/xlsx 用 MinerU 原生 OOXML 转换（秒级、无需模型/LibreOffice），pdf/扫描件用 MinerU pipeline（OCR/版面/表格）；引擎不可用或返回空时降级 python-pptx/python-docx/openpyxl/pdfplumber 即时解析，再降级 MinerU 云 API）。**文件名/目录仅用于预判该不该读，答案必须基于正文**——pptx/docx 等办公文档正文可读后，智能体不再只靠文件名猜测内容。详见下文「文档解析引擎（本地 MinerU）」。
  - SOUL.md / SKILL.md 规定检索顺序：**先预判目录再进目录**（`knowledge_search` 与 `browse(map)` 并行 → `browse(list)` 列目录、在线文档优先精读、办公文档其次 → `search` 补漏），**每读完一篇立即确认能否回答**（能答即停，禁止无差别顺序读完整个目录），钉钉命中文档**必须读正文**再作答、禁止仅给链接；另规定双源穷尽检索（同义词/上下位词/拆解子问题，3-5 次以上）、多源结果归纳合并去重、答案第一句直接是实质内容；钉钉未配置时工具返回明确提示让智能体仅用知识库检索结果作答。
- **引用来源过滤**：[deerflow_runner.py](services/kb-api/app/services/agent/deerflow_runner.py) 的 `_filter_cited_citations` 在 `final` 事件前按答案正文过滤——只保留文档标题核心名（去 `[钉钉]` 前缀/扩展名/`杰克_` 前缀）出现在答案中的引用，避免把仅检索过但答案未提及的文档列出。`final.result.retrieval_all` 同时携带**全量召回快照**（含未被引用的分段，`cited` 标记是否被答案引用），前端持久化到 `chat_messages.detail.retrieval`，「知识运营 → 问答明细」据此展示「召回 N 条，其中被引用 M 条」并逐条标注已引用/未引用。
- **检索源级容错（fail-soft）**：`/internal/kb/retrieve` 对 RAGFlow 检索失败不再整轮 502——多库合并请求遇「无权数据集」（RAGFlow `code 102`，常见于 API Key 与库归属租户不匹配）时自动降级为**逐库检索**，坏库跳过、好库照常返回；整体引擎异常也只降级为 `warnings` 字段随响应透传（sidecar `kb_tools` 转发给智能体），不阻断 Dify/本地结果。单库失败原因可在「知识库 → 检索测试」直接定位。
- **会话模型**：前端每个对话会话生成 `session_id`（重新对话时刷新），kb-api 映射为 DeerFlow `thread_id = kge-u{uid}-{sid}`；同一会话复用 sqlite 检查点维持多轮记忆与上下文。
- **限时探索机制（1 分钟询问 / 5 分钟再问 / 10 分钟兜底）**：由 [exploration_timeout_middleware.py](services/deerflow/backend/packages/harness/deerflow/agents/middlewares/exploration_timeout_middleware.py) 按 `thread_id` 跟踪墙钟（类级注册表，跨"暂停→续跑"保持），到点动作**确定性短路**（不依赖模型自觉、不额外消耗 token）：检索满 **1 分钟**未出答案时，`wrap_model_call` 直接合成一次 `ask_clarification` 工具调用（由 ClarificationMiddleware 拦截后结束本轮 SSE），前端弹出选择按钮「继续探索（再给我一些时间）/ 先基于已检索内容回答」；用户选继续后续跑，满 **5 分钟**仍无答案再问一次；总时长满 **10 分钟**则合成最终答复，明确告知"当前知识库无法获得准确答案"并建议换问法/知识征集。用户选「先基于已检索内容回答」时，`before_model` 注入停止指令给模型一次整合作答的机会，模型仍调工具则由 `after_model` 剥除并给兜底结论。阈值可用环境变量覆盖：`KGE_EXPLORE_FIRST_ASK`（默认 60s）、`KGE_EXPLORE_SECOND_ASK`（300s）、`KGE_EXPLORE_HARD_LIMIT`（600s）。
- **SSE 链路**：浏览器 → kb-api `/api/v1/search/chat/stream` → sidecar `/v1/chat/stream`（SSE）。[deerflow_runner.py](services/kb-api/app/services/agent/deerflow_runner.py) 把 DeerFlow 事件转译为前端协议：步骤事件（🦌 智能体 / 📚 知识检索(第N次) / 📌 钉钉知识库检索 / 📖 读取钉钉文档 / ✅ 检索完成 / 🧩 子任务派发 / 🙋 等待确认）+ `answer_delta` 流式文本 + `final`（答案/引用/追问/**usage token 用量**）；限时探索询问时额外下发 `choice`（问题+选项，渲染选择按钮）与 `choice_pause`（暂停收尾：本轮无 `final`，携带已耗 usage），用户点选后以同一 `session_id` + `action=continue|stop` 发起新请求续跑（[search.py](services/kb-api/app/routes/search.py) 的 `ChatIn.action` → runner → sidecar `ChatStreamIn.action` → `ExplorationTimeoutMiddleware.begin()`）。sidecar 侧按消息 id 去重并以首个状态快照建立历史基线避免重放；同时做**过渡独白 + 规划段落过滤**——工具调用前的 AI 思考文本按消息 id 缓冲并在确认 tool_call 后丢弃；最终答案消息完整缓冲到 values 快照到达，按段落剥离含"用户询问/已执行搜索/任务背景/node_id/工作空间"等关键词的规划段落后，再分块下发；被中间件强制收尾/剥除工具调用的答复由流末兜底逻辑保证必定下发，保证输出直接是答案正文、无内部分析过程。
- **优雅回退**：sidecar 不可达或未配置 LLM 时，kb-api 自动回退到内置 LangGraph 工作流（[workflow.py](services/kb-api/app/services/agent/workflow.py)：问题分类 → 改写 → Dify 检索 → 充分性判定 → 生成 / 深度探索），功能不中断。内置工作流同时是智能体配置页的数据源。
- **能力开关**：`planning_enabled`（Todo 规划中间件）、`subagent_enabled`（Lead Agent 并行派发 Sub-Agent 调研）、`long_memory_enabled`、深度思考等均在「智能体配置」页控制，随请求下发给 sidecar。

本地启动 sidecar：`bash scripts/start-deerflow.sh`（自动准备 Python 3.12 uv 环境）；Docker 全量模式下 `docker-compose.app.yml` 的 `deerflow` 服务自动启动（[services/deerflow/Dockerfile](services/deerflow/Dockerfile)）。

- **文档解析引擎（本地 MinerU）**：二进制办公/PDF 文档（ppt/pptx/doc/docx/xls/xlsx/pdf）由独立的 **MinerU 解析服务**统一转为 Markdown，供 `dingtalk_read_doc` 与本地文档入库 worker（[worker.py](services/kb-api/app/worker.py)）复用。服务为自托管 **MinerU 3.x `mineru-api`**（[scripts/start-mineru.sh](scripts/start-mineru.sh)，独立 venv `services/mineru/.venv`，端口 **2028**，与 DeerFlow sidecar 同模式）：`bash scripts/start-mineru.sh`（首次自动 `uv` 安装 `mineru[core]`；国内默认 `MINERU_MODEL_SOURCE=modelscope`，macOS Apple Silicon 自动 MPS、无 GPU 回退 CPU）。
  - **Office（pptx/docx/xlsx）走 MinerU 原生 OOXML 转换**，纯 Python 实现、秒级返回，**无需下载模型、无需 LibreOffice**；pdf/扫描件走 MinerU `pipeline`（版面/表格/OCR），首次解析时自动下载模型。
  - 调用链：kb-api [agent_internal.py](services/kb-api/app/routes/agent_internal.py) `/internal/dingtalk/content` 下载文件 → [mineru_client.py](services/kb-common/kb_common/clients/mineru_client.py) `parse()` **优先本地引擎**（`POST {MINERU_LOCAL_URL}/file_parse`，multipart `files` + `return_md=true` + `backend=pipeline`，取 `results[].md_content`，默认 `http://127.0.0.1:2028`，可用 env/`mineru_local_url` 关闭或改址）；本地不可用/返回空时**降级本地即时解析**（[agent_internal.py](services/kb-api/app/routes/agent_internal.py) `_local_parse`：python-pptx 逐页抽形状/表格/组合/备注、python-docx 段落+表格+文本框、openpyxl、pdfplumber），再降级 **MinerU 云 API**（需 `MINERU_API_KEY`）。健康检查 `GET http://127.0.0.1:2028/health`。
  - 效果：钉钉「战略报告.pptx」「经营计划 BP.docx」等可在 1–2 秒读出正文（含三大战略等关键内容），智能体据此基于**文档正文**作答而非文件名猜测。

**流式问答**：智能问答页支持 SSE 流式输出。DeerFlow 底座下实时显示 Lead Agent 运行进度（🦌 DeerFlow 2.0 智能体 → 📚 知识检索（第 N 次，含检索词）→ 📌 钉钉知识库检索/目录浏览 → 📖 读取钉钉文档 → ✅ 检索完成 → 🧩 子任务派发/返回 → 🙋 等待确认），回答文本通过 `answer_delta` 事件**逐字流式上屏**（不再等终帧）；回退内置工作流时显示原节点进度（🔍 分类 → ✏️ 改写 → 📚 检索 → ⚖️ 判定 → ✍️ 生成）。后端端点 `POST /api/v1/search/chat/stream`，前端 [chat/index.vue](web/src/views/chat/index.vue) 消费 SSE 事件逐条渲染步骤与增量文本。每个前端会话携带 `session_id`（重新对话时刷新）映射 DeerFlow thread。引用来源默认只展示前 5 条，超过可「展开全部」。模型下拉只显示系统配置中已配置可用的 LLM（未配置时显示占位引导）。

- **检索过程可展开**：助手消息的「智能体检索过程」步骤条运行中显示为紫色高亮条（spinner + 当前步骤 +「▸ 展开过程」），**点击即可展开查看全部步骤**（最后一步进行中带 spinner）；问答结束后步骤条仍可随时展开/收起，便于回溯检索路径。注意轮次保存后仅刷新侧边栏会话列表，不用 DB 记录全量重载消息，以保留按钮/Token 等交互态。
- **限时探索选择按钮**：检索超 1 分钟（继续后再超 5 分钟）时，助手消息中出现选择卡片（`choice`/`choice_pause` 事件），提供「继续探索（再给我一些时间）/ 先基于已检索内容回答」两个按钮：点继续则以 `action=continue` 在同一 thread 续跑；点停止则 `action=stop` 立即收尾作答；按钮点击后禁用。**确认卡片 10 秒未点选自动按「继续探索」续跑**（按钮文案精简为「继续探索」等、不叠加倒计时，倒计时仅在按钮下方提示行「N 秒内未选择将自动继续探索」展示，超时后续跑并标注「已自动继续探索」；后台会话的确认态同样到点自动续跑，续跑消息写入该会话自己的消息数组；续跑轮不把选项文字作为用户消息上屏，历史重载时同样隐藏）。10 分钟硬上限由系统自动给出"当前知识库无法获得准确答案"的兜底回复。
- **Token 消耗展示**：消息时间行在模型名/时间之后显示本轮模型接口返回的真实用量（`· ↑{input_tokens} ↓{output_tokens} tokens`，≥1000 紧凑显示为 x.xk），数据取自 sidecar `end` 事件的 `usage`（按各模型轮次 `usage_metadata` 累计），经 runner `final.result.usage` / `choice_pause.usage` 透传到前端。
- **多会话并行问答（切换不中断，最多 5 路）**：不同会话可同时问答，**切换会话不中断进行中的回答**。前端 [chat/index.vue](web/src/views/chat/index.vue) 用会话级运行注册表 `runs`（sid → 消息对象/abort/状态）与内存消息数组 `liveArrays` 持有各会话的 SSE 连接与流式消息——连接归属会话而非视图，切换会话仅替换 `messages` 引用，后台会话继续逐字上屏，切回即恢复现场；当前会话是否「运行中」由 `loading` 计算属性派生。侧栏会话条目状态指示：运行中=**旋转小圆圈**（`.run-spinner`）、等待确认（choice_pause 暂停）=**琥珀点**（`.run-choice-dot`，回会话点选继续/停止）、有新完成的回答=**绿点**（`.run-done-dot`，打开该会话即清除）。并发上限 `MAX_CONCURRENT=5`（等待确认的 choice 态不占后端并发），超出发送提示稍候；同一会话运行中不允许重复发送。仅「停止按钮」与「删除会话」会 abort + cancel 对应会话。实现要点：`reactive(new Map())` 读取值是响应式代理，与原始对象严格比较恒为 false，运行态身份判断必须经 `toRaw` 归一（`isRun` 辅助函数），否则转圈不落点、完成不清理。后端配套：sidecar 的全局 `_stream_lock` 改为 `_NullLock`（仅保留结构），并发由 `_serialize_checkpointer` 给 SqliteSaver 各方法挂**方法级 `threading.RLock`**（可重入，规避 `get_tuple`→`setup` 嵌套自锁）实现——多会话的 LLM 流式长任务完全并行，仅 sqlite 检查点短 IO 在共享连接上串行；多会话同时读钉钉文档时 [agent_internal.py](services/kb-api/app/routes/agent_internal.py) `_run_dws` 全局并发≤3 + 0.4s 节流 + 瞬时失败重试 1 次，避免并行限频 500。
- **运行中可中断（停止按钮 + 后端真正停止 Agent）**：回答进行中发送按钮变为深色实心方块（停止按钮），点击即中断；中断后按钮恢复发送态，末条 AI 消息标记「⏹ 已中断 · {model}」（无正文时补「（已中断本次回答）」）。中断采用**双保险**：前端除 `abort` 断开 SSE 外，额外 `POST /api/v1/search/chat/cancel`（[chat.ts](web/src/api/chat.ts) `cancelChat`），kb-api 按同一规则拼 `thread_id` 调 sidecar `POST /v1/chat/cancel`（[deerflow_runner.py](services/kb-api/app/services/agent/deerflow_runner.py) `cancel_deerflow_stream`），确保取消信号确定性到达、不依赖断连检测时机。后端确定性停止：① [exploration_timeout_middleware.py](services/deerflow/backend/packages/harness/deerflow/agents/middlewares/exploration_timeout_middleware.py) 的 `cancelled` 标志在每个模型节点边界由 `wrap_model_call` 短路（**不再调用模型、不耗 token**），`_due_action` 中 cancelled 优先级最高；② sidecar 收到取消后在**驱动 agent 的同一线程** `gen.close()` 注入 `GeneratorExit`，关闭同步图迭代器，中断进行中的模型流并释放资源。sidecar `/v1/chat/stream` 用**专属 worker 线程 + `asyncio.Queue` 泵**驱动**同步** `agent.stream()`（DeerFlow harness 用同步 `SqliteSaver`，不支持 `astream`/`AsyncSqliteSaver`）：worker 线程在 loop 外迭代同步生成器、把帧 `call_soon_threadsafe` 入队，async 泵端出帧下发；泵每 0.2s 轮询 `is_cancelled(thread_id) or request.is_disconnected()`，使取消发生在「等帧间隙」（无帧可触发）或**纯断连**（不调 cancel 直接关页/杀连接）时也能被检测并停图。`gen.close()` 始终在同一 worker 线程的 finally 中执行，保证 checkpointer 串行锁必被释放（多会话并发后 `_stream_lock` 退化为 `_NullLock`，sqlite 串行由 `_serialize_checkpointer` 的方法级 RLock 承担，见「多会话并行问答」）——中断后立即发新问答不卡锁、不回退内置工作流。sidecar 下发 `{"type":"cancelled"}` 帧，runner 收到即 `return`（跳过追问生成/`final`），kb-api `_gen` 的 finally 兜底再 cancel 一次（正常结束为 no-op）。注意 anyio 的 `CancelScope` 是**同步**上下文管理器，收尾时必须用 `with anyio.CancelScope(shield=True):`（`async with` 会抛 `TypeError` 导致 close 不执行、图被遗弃在后台继续跑空转耗 token）。同步工具（`@tool` 知识库/钉钉检索，httpx 同步、在 executor 线程跑）无法即时取消但会自然结束且不再驱动图；模型调用是 async httpx（token 消耗源），由中间件短路 + close 迭代器在「下一个图事件」粒度（通常 1～数秒）停止。

**HiAgent 智能问答（外链嵌入）**：侧边栏「HiAgent智能问答」页通过火山引擎 HiAgent WebSDK（`embedFull.js`）以 iframe 方式内嵌官方智能体对话界面，见 [hiagent/index.vue](web/src/views/hiagent/index.vue)。SDK 以初始化内联脚本的父节点作为挂载容器，前端在 `onMounted` 时把 SDK 脚本与 init 脚本动态注入页面宿主 div，离开页面时清理 iframe；appKey/baseUrl 常量定义在该视图文件顶部。

**钉钉 DEAP 智能问答（外链嵌入）**：侧边栏「DEAP智能问答」页（位于「智能问答」下方）以 iframe 直接嵌入钉钉 DEAP 平台发布的智能体 H5。**嵌入链接在「智能体配置 → DEAP智能问答」页签维护**（粘贴发布后的 H5 链接，与 HiAgent / Dify 嵌入配置同结构，存 settings 表 `agent_config.external_agents.deap`，见 [deap-agent/index.vue](web/src/views/deap-agent/index.vue)；未配置时回退内置默认链接）。**需钉钉登录态**：未登录时 DEAP 会 302 到钉钉 OAuth（`login.dingtalk.com`），部分浏览器会在 iframe 内拦截跳转/扫码，页面工具栏提供「在钉钉中打开 ↗」按钮新页签打开完成登录。显隐由「系统配置 → 菜单配置」开关控制。

**菜单显示配置**：「系统配置 → 菜单配置」以列表形式管理侧边栏「功能区」全部菜单的显示/隐藏（开关即存，整体覆盖式更新）。「智能问答」为默认首页固定显示。存储：settings 表 `menu_visibility`（JSON `{"hidden":[路径]}`），接口 `GET/PUT /api/v1/settings/menu-visibility`（GET 所有登录用户可读、PUT 管理员），见 [settings_route.py](services/kb-api/app/routes/settings_route.py) 与 [model.vue](web/src/views/governance/model.vue)「菜单配置」Tab；[Sidebar.vue](web/src/components/layout/Sidebar.vue) 加载配置过滤功能区菜单（含二级子菜单），读取失败或未配置时默认全部显示，隐藏仅作用于导航（路由仍可直接访问）。

**检索返回脱敏策略控制台（知识应用 → 脱敏策略，`/apply/masking`）**：管理「检索返回策略」而非数据本身——管理员改任何配置都能回答：改变哪次检索、链路哪个节点、对哪类用户、把什么敏感实体、变成什么返回形态。框架为**条件 → 识别 → 动作 → 执行节点 → 兜底 → 审计**。页面五页签：策略编排 / 敏感词典 / 豁免管理 / 脱敏审计 / 预览沙箱，顶部全局策略条（总开关 / 送LLM前脱敏 / 输出后二次过滤 / 失败策略 / 用户提示语「部分内容因权限隐藏」，存 settings 表 `masking_global`）。

- **数据模型**：`masking_policies`（作用域 global/library/kb 继承叠加 + 角色/场景条件 + 词典类型/自定义正则/上下文规则三类识别 + 实体类型→动作映射 + 双执行节点开关 + 失败策略）、`masking_exemptions`（人 + 范围 + 实体类型 + 有效期，到期自动失效）、`masking_logs`（脱敏审计：命中规则标签与数量，**不含敏感原文**），迁移 [0028_masking_policies.py](alembic/versions/0028_masking_policies.py)。
- **脱敏引擎**：[masking.py](services/kb-api/app/services/masking.py)。识别＝内置正则（手机号/身份证/银行卡/邮箱，身份证先于银行卡匹配防吞）＋敏感词典（复用 `sensitive_items`，TTL 30s 缓存）＋上下文规则（如「薪酬」邻近金额）＋策略自定义正则（非法正则跳过不阻断）；动作＝部分遮蔽/泛化/替换/哈希/截断/拒绝返回，**多策略命中同一实体类型取最严格**；**一致性替换**——同一实体文本在同一请求内映射到同一确定性令牌，防多片段拼接还原；`load_mask_context` 无策略或全局关闭时返回 None，调用方零开销直通；审计写入为 fire-and-forget（独立 session，异常不影响主流程）。
- **执行节点**：①**送LLM前（底线）**——[agent_internal.py](services/kb-api/app/routes/agent_internal.py) `/internal/kb/retrieve` 返回出口按 `thread_id` 回溯提问用户身份（角色/豁免）后对召回片段脱敏（reject 动作整条丢弃），kb_tools 透传 thread_id；②**输出后二次过滤**——统一检索结果与问答流式输出（[search.py](services/kb-api/app/routes/search.py)）再扫描，命中可阻断/替换/降级。
- **API**：[masking.py](services/kb-api/app/routes/masking.py)（全部仅 super_admin/admin）：`GET/POST/PUT/DELETE /api/v1/masking-policies`、`GET/POST/DELETE /api/v1/masking-exemptions`、`GET /api/v1/masking-logs`（场景/节点筛选 + 分页）、`GET/PUT /api/v1/masking-global`、`POST /api/v1/masking/sandbox`（模拟查询+模拟角色 → 真实检索（本地 ES + Dify/RAGFlow 镜像，fail-soft）→ 原始 vs 脱敏对比 + 命中规则汇总，不调 LLM）。策略校验：重名 409、非法正则/作用域/动作 422。
- **前端**：[masking/index.vue](web/src/views/masking/index.vue)（全局策略条 + 五页签）+ [PolicyTab.vue](web/src/views/masking/PolicyTab.vue)（编排弹窗：基本信息/触发条件/识别/动作/执行节点四区分节）、[DictTab.vue](web/src/views/masking/DictTab.vue)（敏感词条 CRUD，即原「敏感信息」页下沉为词典来源，[api/sensitive.ts](web/src/api/sensitive.ts) 保留）、[ExemptionTab.vue](web/src/views/masking/ExemptionTab.vue)（豁免 + 限期开关）、[AuditTab.vue](web/src/views/masking/AuditTab.vue)（筛选 + 分页 + 命中规则标签）、[SandboxTab.vue](web/src/views/masking/SandboxTab.vue)（左右对比卡：原始召回 vs 用户视角脱敏后，逐片段命中规则/拒绝标记）。路由 `/apply/masking`（菜单「脱敏策略」）替换原 `/apply/sensitive`，仅 super_admin/admin 可见。
- **验证**：端到端脚本 [test_masking_e2e.py](scripts/test_masking_e2e.py)（TestClient 直连本地基础设施）：登录鉴权 → 全局配置读写 → 策略 CRUD（重名 409/非法正则 422）→ 沙箱 → 送LLM前脱敏（thread_id 身份回溯 + viewer 兜底 + 片段/标题脱敏）→ 审计落库与日志自身脱敏 → 豁免创建/撤销 → 清理，输出 `ALL E2E PASSED`。原「知识应用 → 应用总览」演示页已移除，`/apply` 重定向到 `/apply/masking`。

**「关于我」产品介绍页**：侧边栏左下角「关于我」按钮（紫色图标，所有登录用户可见，[Sidebar.vue](web/src/components/layout/Sidebar.vue)），点击**新开浏览器页签**打开纯静态介绍页 [web/public/about.html](web/public/about.html)（原生 `<a :href="import.meta.env.BASE_URL + 'about.html'" target="_blank">`，避免弹窗拦截；dev 与构建产物均由根路径直达，不依赖登录态与后端）。页面以营销页形式呈现：平台定位与数据条（4 应用进程 / 5 基础设施容器 / 1.6万+ 钉钉知识节点 / 6.7TB 企业存储 / 1/5/10min 限时探索）、设计理念（答案优先而非链接堆砌、先预判目录再进目录、把时间还给用户、一切皆可配置、安全与稳定是默认值、基于正文作答）、**知识治理闭环全景图**（单张紧凑白色卡片，SVG 环形闭环布局：5 个彩色圆角节点①采集「汇得拢」②加工「读得懂」③应用「用得上」④运营「看得见」⑤治理「管得住」围成圆环，灰色实线弧箭头沿环顺时针流转，**治理→采集的橙色收口弧使首尾相接成闭环**；环中心为紫色「治理飞轮」hub（越用越准·越治越新）；环内两条红色虚线表达跨环节质量回流——治理→加工（重分段/重打标/重建索引）、应用→治理（问答反馈·点赞/踩/无结果），回流标签最后绘制并加白色描边保证压线可读；卡片底部双行图例（环节色点 + 线条类型）；≤960px 窄屏自动降级为纵向紧凑列表）、闭环整体价值三胶囊（知识不沉没/质量自进化/投入可度量）、分层系统架构图（接入层 → DeerFlow 智能体层 → 应用服务层 → 检索与解析 → 基础设施）、八大功能模块卡片与技术亮点清单；内容与 README 架构章节保持同口径（HiAgent 为临时模块未收录）。纯单文件 HTML + 内联 CSS，无外部资源依赖，响应式适配窄屏。

## 两种运行模式

### 模式 1（推荐）：本地原生 + Docker 基础设施

应用进程跑在 Mac 原生内存（模型全部外接 HTTP API，无本地模型内存占用），基础设施跑在 Docker。**开发期推荐**，迭代快。

```bash
# 1. 启动 Docker 基础设施（dev-services compose，一次性；路径按实际调整）
docker compose -f ~/Documents/03_Resource/开发环境/docker-compose.yml up -d

# 2. 配置 .env（仓库根目录，参考下方 .env 配置）

# 3. 安装依赖（每个服务首次）
cd services/kb-common && uv sync && cd -
cd services/kb-api && uv sync && cd -
cd services/faq-service && uv sync && cd -

# 4. 初始化数据库表 + 种子 admin
cd services/kb-common && uv run alembic -c ../../alembic.ini upgrade head
cd services/kb-common && uv run python ../../scripts/seed_admin.py
# admin / admin123

# 5. 启动 4 个应用进程（各开一个终端）
cd services/kb-api && uv run uvicorn app.main:app --reload --port 8000
cd services/kb-api && uv run celery -A app.worker worker -Q ingestion --concurrency=2 -l info
cd services/faq-service && uv run uvicorn app.main:app --reload --port 8004
bash scripts/start-deerflow.sh       # DeerFlow 2.0 QA sidecar:2027（首次自动 uv 准备 Python 3.12）
bash scripts/start-mineru.sh         # 本地 MinerU 文档解析引擎:2028（首次自动 uv 安装 mineru[core]；解析 ppt/pptx/docx/xlsx/pdf 正文）
# 不启动 sidecar 也可问答：kb-api 健康检查失败时自动回退内置 LangGraph 工作流
# 不启动 MinerU 也能读办公文档：自动降级 python-pptx/python-docx 等即时解析（图片型/扫描件效果弱于 MinerU）

# 6. 启动前端
cd web && pnpm install && pnpm dev   # http://localhost:3000
```

登录：`admin` / `admin123` (super_admin)。

### 模式 2：Docker 全量模式（需基础设施已启动）

**前提**：基础设施（dev-services compose）必须已启动，参考模式 1 步骤 1。

```bash
# 启动应用服务（kb-api / faq-service / kb-worker / deerflow sidecar）
docker compose -f docker-compose.app.yml up -d --build
```

> 模型全部外接（向量/重排/LLM 走 HTTP API，见 `.env.docker` 的 `EMBEDDING_*` / `RERANK_*`），kb-api 容器无本地模型加载，colima 无额外内存要求，启动即就绪。

清理应用容器（不清理基础设施）：
```bash
docker compose -f docker-compose.app.yml down
```

### 模式 3：内网服务器部署（独立 Linux 服务器）

把完整服务部署到一台内网 Linux 服务器（无 Docker、无公网，端口可能被其他服务占用）。与模式 2 的区别：基础设施与应用都在服务器上跑 Docker，前端由服务器 nginx 托管（非 vite dev），并针对**内网服务器的硬约束**做了适配。

**服务器硬约束与适配**（已内置到依赖 / Dockerfile，无需额外手工处理）：

| 约束 | 适配 |
|------|------|
| 老 CPU（仅 x86-64-v1，缺 SSE4.1/POPCNT） | `kb-common` 固定 `numpy<2`（NumPy 2.x 要求 x86-64-v2，会 `Illegal instruction`/`cannot load module`）；minio 固定旧版镜像 `RELEASE.2022-12-12T19-27-27Z`（新镜像要求 x86-64-v2） |
| 内存偏小（<8GB） | **本地模型已全部停用**（向量/重排外接 HTTP API，kb-api 内存从 ~2-3GB 降到 ~160MB）；kb-worker 以 `EMBED_VIA_INTERNAL=true` 经 kb-api 统一转发外接向量 API（见 [indexer.py](services/kb-common/kb_common/rag/indexer.py)）；ES `-Xms512m -Xmx512m`；服务器加 8G swap 兜底 |
| 内网网络（docker.io 超时 / HTTP 透明代理污染） | 三个 Dockerfile 内 `UV_INDEX_URL` 走阿里云 pypi；Docker Hub 配 `registry-mirrors`（daocloud 等）；apt 源 / pypi 一律用 **HTTPS**（内网对 HTTP 常有透明代理导致 Hash Sum mismatch） |
| containerd 并发拉取 overlayfs 偶发 `device or resource busy` | **串行**拉取 / 构建（一次一个服务），失败重试即绕过 |

**部署步骤**（已在 `10.10.166.2`（Ubuntu 22.04，jack）验证，服务器工作目录统一 `/opt/kge`；部署文件在仓库 `deploy/intranet/`）：

```bash
# 0. 安装 Docker（HTTPS 阿里云源 + 镜像加速器）
#    - /etc/apt/sources.list 换成 https://mirrors.aliyun.com/ubuntu/
#    - docker-ce 源 https://mirrors.aliyun.com/docker-ce/linux/ubuntu
#    - /etc/docker/daemon.json 配 registry-mirrors（docker.m.daocloud.io 等）
#    - 加 8G swap：fallocate -l 8G /swapfile && mkswap && swapon

# 1. 上传仓库到服务器（build context = repo root；排除本地 venv/依赖/前端源码）
rsync -a --exclude '.venv' --exclude '__pycache__' --exclude 'node_modules' \
  --exclude 'web/src' --exclude 'services/*/tests' --exclude 'services/*/data' \
  . jack@<服务器IP>:/opt/kge/

# 2. 模型全部外接（本次更新：停用本地 BGE）：无需再物化模型文件！
#    在 .env.app 配 EMBEDDING_BASE_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL
#    （与可选 RERANK_API_URL / RERANK_API_KEY / RERANK_MODEL）；
#    LLM/MinerU Key 也可部署后在「模型配置」页补填
# 3. 准备前端产物：本地 `cd web && pnpm build`，把 dist/ 上传到 /opt/kge/web/dist（nginx root）

# 4. 基础设施（postgres/redis/elasticsearch/minio/kkfileview）
cd /opt/kge/deploy/intranet
cp .env.infra.example .env.infra && cp .env.app.example .env.app   # 填真实随机密码
docker compose --env-file .env.infra -f docker-compose.infra.yml up -d

# 5. 构建应用（串行，规避 containerd overlayfs bug；kb-worker 复用 kb-api 镜像）
docker compose -f docker-compose.app.yml build kb-api
docker compose -f docker-compose.app.yml build faq-service
docker compose -f docker-compose.app.yml build deerflow

# 6. 启动应用（kb-api:8001 / faq-service:8004 / deerflow:2027 / kb-worker）
docker compose -f docker-compose.app.yml up -d

# 7. 初始化 DB + 种子 admin
docker exec kge-kb-api sh -c "cd /app/services/kb-api && uv run alembic -c ../../alembic.ini upgrade head"
docker exec kge-kb-api sh -c "cd /app/services/kb-api && uv run python ../../scripts/seed_admin.py"
#    ⚠️ 模型变更必须伴随迁移：新增/修改 ORM 字段时先在本地补 alembic 迁移再部署，
#    禁止依赖本地 create_all/手工改表（schema 漂移会导致线上 UndefinedColumnError 500），
#    迁移号先 `alembic heads` 确认无并行冲突。部署后用受影响接口验证。

# 8. 前端 nginx（8080 端口，反代 /api/v1/faq→8004、/api/v1→8001）
sudo cp nginx-kge.conf /etc/nginx/sites-enabled/kge && sudo nginx -t && sudo systemctl reload nginx

# 9. 开放防火墙（ufw 只放行前端 8080；宿主机端口默认被 ufw 拦截）
sudo ufw allow 8080/tcp comment "KGE 前端"
```

> **注意 ufw 与 Docker 的交互**：ufw 规则只作用于宿主机进程监听的端口（nginx 8080、kb-sync 80）；Docker 发布的端口（8001/8004/2027/8012/9000-9001/9200/5432/6379）经 iptables PREROUTING/FORWARD 链 DNAT，**会绕过 ufw 直接可访问**。若需限制这些端口，需另行配置 `DOCKER-USER` 链或去掉 compose 的宿主端口映射。

部署文件清单（`deploy/intranet/`）：`docker-compose.infra.yml`（基础设施）、`docker-compose.app.yml`（应用，build context 自动指向上两级 repo root）、`.env.infra.example` / `.env.app.example`（脱敏模板，部署时复制为 `.env.infra` / `.env.app` 填真实密码与外接模型 API 配置）、`nginx-kge.conf`（前端反代）。模型全部外接，无需物化模型文件；旧部署里的 `deploy/intranet/models/` 目录与 `BGE_EMBED_MODEL` / `BGE_RERANK_MODEL` 环境变量已废弃，升级时直接删除。

**⭐ 本次更新重点（2026-09 推送服务器必读）**

本轮优化以「停用本地小模型 + 全链路提速」为核心，**下次推送服务器时按以下清单操作**：

1. **停用本地 BGE-M3 + bge-reranker（模型全部外接）**——kb-api 不再加载本地模型：
   - 内存：worker 进程 ~2-3GB → ~160MB；镜像不再包含 torch/FlagEmbedding（`deploy/intranet/models/` 与 `BGE_EMBED_MODEL` / `BGE_RERANK_MODEL` 已从 compose 移除）。
   - **服务器升级操作**：① `.env.app` 新增 `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL`（OpenAI 兼容端点，模型须 1024 维以对齐既有 ES 索引）与可选 `RERANK_API_URL` / `RERANK_API_KEY` / `RERANK_MODEL`；② 删除服务器上旧的 `deploy/intranet/models/` 目录；③ **必须重新 build 镜像**（依赖已瘦身）。不配置外接向量模型时本地 RAG 检索/入库不可用（Dify/RAGFlow 知识库检索不受影响），重排未配置则自动跳过。
2. **XXL-Job 定时同步调度上线 + 启动降级**：XXL-Job admin 不可达时 kb-api 自动降级（记录日志、跳过定时调度），不再因调度器缺席拖死整个服务；本地开发可在 `.env` 设 `XXL_JOB_ENABLED=false`。生产启用定时同步需另起 `deploy/xxl-job/`（admin + MySQL）。
3. **数据库迁移新增 0026-0030**（sync_tasks / 敏感项 / 脱敏策略 / 同步源绑定 XXL 执行器 / rerank_profiles 多配置）：升级后必须 `alembic upgrade head` 并验证同步与问答接口；rerank 多配置表为空且旧单值已配置时自动建档并标记生效。
4. **同步链路质量与性能**：① 源文档**原样直传 Dify**（不再经本地转换引擎，先备份 MinIO 再上传）；② 钉钉目录树并发遍历（≤3 并发）+ 服务端 10 分钟缓存，「同步列表」从数十秒降到秒级（缓存命中 <100ms）；③ 逐源统计 N+1 改批量聚合接口 + 前端 1 小时持久缓存（`kge:sync_sources_page_v1` / `kge:dify_datasets_v1` / `kge:dingtalk_workspaces_v1`）。
5. **升级后老规矩**：app 容器重启后 `docker restart` nginx（避免上游 IP 缓存导致 502）。

**访问**：`http://<服务器IP>:8080`，登录 `admin` / `admin123`。

**端口规划**（服务器常已有服务占 80/8000，需错开）：

| 服务 | 宿主端口 | 容器内 |
|------|---------|--------|
| 前端（nginx） | 8080 | — |
| kb-api | 8001 | 8000 |
| faq-service | 8004 | 8004 |
| deerflow | 2027 | 2027 |
| postgres / redis / es / minio / kkfileview | 5432 / 6379 / 9200 / 9000 / 8012 | 同左 |

> 部署完成后仍需在「模型配置」页补 **LLM API Key**（智能问答，否则 deerflow bootstrap 返回 409「LLM not configured」）与 **MinerU 云 API Key**（PDF/Office 解析，未配仅支持 txt/md/csv 本地解析）。向量/重排模型全部外接：`.env.app` 配 `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` 与可选 `RERANK_API_URL` / `RERANK_API_KEY` / `RERANK_MODEL`（本地 BGE 模型与 `models/` 卷挂载已停用）；未配置时本地 RAG 检索/入库不可用（Dify/RAGFlow 检索不受影响），rerank 未配置则检索跳过重排。

## .env 配置

仓库根目录 `.env`（开发模式用）：

```env
DATABASE_URL=postgresql+asyncpg://dev:dev123456@127.0.0.1:5432/dev_db
REDIS_URL=redis://:dev123456@127.0.0.1:6379/0
ES_HOST=http://127.0.0.1:9200
MINIO_ENDPOINT=127.0.0.1:9000
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin
KKFV_URL=http://127.0.0.1:8012
JWT_SECRET=kb-mvp-dev-secret
# LLM（可选 - 留空则问答返回友好提示）
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
LLM_API_KEY=
LLM_MODEL=glm-4-flash
# MinerU（可选 - 配置 Key 后支持 PDF/DOCX/PPTX/XLSX/HTML 云解析；未配置仅本地解析 txt/md/csv）
MINERU_API_KEY=
# Dify 知识库（智能问答 Agent 使用；Service API 端点需含端口与 /v1）
DIFY_BASE_URL=http://127.0.0.1:8088/v1
DIFY_API_KEY=
DIFY_DATASET_IDS=
```

**Dify 配置说明**：
- `DIFY_BASE_URL`：Dify **Service API** 端点，必须同时包含端口与 `/v1`，如本地 `http://127.0.0.1:8088/v1`（见下方 [Dify 知识库服务](#dify-知识库服务)）。注意不是控制台地址 `http://127.0.0.1:8088`（少 `/v1`）也不是 `http://127.0.0.1/v1`（少端口），两者都会导致数据集列表/检索失败。
- `DIFY_API_KEY`：Dify Dataset API Key（如 `dataset-R6qPqf7CfPC0Dbn6uT3LxIKw`）。留空则智能问答走本地 ES 知识库；配置后走 LangGraph Agent + Dify 检索。可在「系统配置」页运行时配置。
- `DIFY_DATASET_IDS`：默认检索的 Dify 数据集 ID，逗号分隔；问答页未选数据集时使用。可在配置页「拉取列表 → 填入全部 ID」自动填入。

Docker 全量模式用 `.env.docker`（compose 自动通过 `env_file` 注入，使用 Docker 服务名连基础设施）：

```env
DATABASE_URL=postgresql+asyncpg://dev:dev123456@dev-postgres:5432/dev_db
REDIS_URL=redis://:dev123456@dev-redis:6379/0
ES_HOST=http://dev-elasticsearch:9200
MINIO_ENDPOINT=dev-minio:9000
KKFV_URL=http://dev-kkfileview:8012
KB_API_INTERNAL_URL=http://kb-api:8000
# ...其余同 .env
```

**LLM_API_KEY / MINERU_API_KEY 可选**：
- `LLM_API_KEY` 留空 → 问答接口返回「LLM 调用失败：... 请在 设置 页配置 LLM API Key」，文档检索/溯源照常工作。
- `MINERU_API_KEY` 配置后 -> PDF/DOCX/PPTX/XLSX/HTML 走 MinerU 云解析；留空 -> 仅 txt/md/csv 本地解析兜底，二进制格式会抛「解析 {ext} 需配置 MINERU_API_KEY」（可在「平台管理 -> 模型设置」页运行时配置，密钥落地 settings 表，GET 接口掩码 `value=""` + `is_set=true`）。

## 冒烟测试

```bash
./scripts/smoke_test.sh
```

脚本自动启动 3 个应用进程（如未运行），覆盖完整链路：
1. 登录 + `/users/me` 字段校验
2. 创建文档知识库
3. 上传文档
4. 等待处理（PENDING → PARSING → INDEXING → COMPLETED）
5. 检索（hybrid: kNN + BM25 + RRF + rerank）
6. 问答（LLM 未配则返回友好提示，非阻断）
7. FAQ KB + 条目 + FAQ 检索（精准 + 语义融合）
8. API Key 创建 + 列表
9. 设置 GET（验证密钥掩码）

预期输出末行：`SMOKE OK`。日志在 `/tmp/kb-smoke/{kb-api,faq-service,kb-worker}.log`。

环境变量：
- `KEEP_SERVICES=1`：脚本结束后保留进程（便于继续调试）
- `EXTERNAL_SERVICES=1`：服务已在外部运行，跳过启动

## 常见问题

| 问题 | 解决 |
|------|------|
| 检索报「未配置外接向量模型」 | 本地 RAG 检索/入库需在 `.env` 配 `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL`（OpenAI 兼容端点，维度须与既有 ES 索引一致）；Dify/RAGFlow 知识库检索不受影响 |
| 检索测试报「向量模型调用失败，请修改后重试」 | Embedding 服务不可达或响应异常（`EmbeddingError`）：检查网络可达性与「系统配置 → 接入配置 → Embedding 向量模型」生效配置；`/search/test` 返回 502 + 该提示（不再 500）。检索测试页对各知识库还会逐库显示可读错误（如「RAGFlow 服务连接失败」「Dify 服务调用失败」） |
| 问答提示「尚未配置 LLM API Key」 | 智能问答依赖 LLM；在「系统配置 → 接入配置」填 LLM API Key 并保存，或在 `.env` 配 `LLM_API_KEY`。Agent 未配置时返回 `config_error=llm_not_configured`（不再 500），前端在对话气泡给出跳转按钮 |
| Rerank 重排配置与切换 | 「系统配置 → 接入配置 → Rerank 重排模型」支持**多条配置 + 单选生效**：`+ 新增重排配置` 弹窗填写服务地址（含完整 `/rerank` 路径）/模型名，API Key **非必填**（内网/自建服务如 Xinference 常免鉴权，测试连通性不再强制 Key）；编辑时 Key 留空保持不变。点行首 radio 切换生效（同名「生效中」标记），生效配置自动回写 `rerank_api_url` / `rerank_api_key` / `rerank_model` 运行值；删除生效配置时自动回退到最早一条，全部删除则清空（检索跳过重排，RRF/BM25 兜底）。首次升级后旧单值配置自动迁移为「默认·旧配置迁移」并标记生效 |
| RAGFlow 多环境配置与切换 | 「系统配置 → 接入配置 → RAGFlow 知识库」支持**多条连接 + 单选生效**（与 Dify/Rerank 同模式，迁移 0032 `ragflow_profiles`）：`+ 新增 RAGFlow 配置` 弹窗填写名称/服务地址（含端口与 `/api/v1` 后缀）/API Key（必填，RAGFlow 接口均需 Bearer 认证），弹窗内可「测试连通性」（编辑态 Key 留空自动回退已存值）；点行首 radio 切换环境（如测试/生产），生效配置自动回写 `ragflow_base_url` / `ragflow_api_key` 运行值，知识源管理与检索/同步链路无需改动；删除生效配置自动回退最早一条。首次升级后旧单值配置自动迁移为「默认·旧配置迁移」并标记生效 |
| Embedding 多模型/多环境配置 | 「系统配置 → 接入配置 → Embedding 向量模型」支持**多条配置 + 单选生效**（迁移 0034 `embedding_profiles`，与 LLM/Rerank 同模式）：`+ 新增配置` 弹窗填写名称/服务地址（OpenAI 兼容，需含 `/v1`）/模型名/API Key（非必填，内网/自建服务常免鉴权），**「拉取模型列表」**可直接从服务拉取候选模型下拉选择（也可手输）；弹窗内「测试连通性」发最小 embed 请求（编辑态 Key 留空回退已存值）；点行首 radio 切换生效，生效配置自动回写 `embedding_base_url` / `embedding_api_key` / `embedding_model` 运行值，本地 RAG 入库/检索链路无需改动；删除生效配置自动回退最早一条。首次升级后旧单值配置自动迁移为「默认·旧配置迁移」并标记生效。注意切换模型须保持向量维度与既有 ES 索引一致（1024 维），否则需重建索引 |
| PDF/DOCX 解析失败 | 配置 `MINERU_API_KEY` 后支持 PDF/DOCX/PPTX/XLSX/HTML 云解析；留空则仅 txt/md/csv 本地解析，二进制格式抛「需配置 MINERU_API_KEY」 |
| `uv sync` 报 `kb-common` 找不到 | 在 `services/kb-common` 先 `uv sync`；Docker 构建已通过 repo-root context 解决 |
| 端口 8000/8004 被占用 | `lsof -i :8000` 找到进程；或改 uvicorn `--port` |
| dev-network 不存在 | 先启 dev-services compose：`docker compose ls` 找配置文件 |
| Dify 镜像拉取 EOF | Docker Hub CDN 间歇中断；用 `bash dify/docker/pull-images.sh` 单镜像重试续传，或重跑 `docker compose up -d` |
| Dify 数据集列表 502/连接失败 | Dify `API 端点` 必须含端口与 `/v1`：本地为 `http://127.0.0.1:8088/v1`。填成 `http://127.0.0.1/v1`（缺端口）或 `http://127.0.0.1:8088`（缺 `/v1`）都会失败；在「系统配置」的 Dify 配置编辑弹窗中更正并重新「启用」 |
| 智能问答数据集为空 | 先在 Dify 控制台创建知识库并上传文档，再到「系统配置 → 接入配置」填 Dify `API 端点`/`API Key` 并「拉取列表」 |
| 8088 端口被占用 | 改 `dify/docker/.env` 的 `EXPOSE_NGINX_PORT`，并同步改 kb-api 的 `DIFY_BASE_URL` |
| Dify 控制台 502 Bad Gateway | nginx 启动早于 api 就绪会缓存错误上游 IP（macOS 代理 fake-ip `198.18.x.x`）；`docker restart docker-nginx-1` 强制重解析即可。若 api 日志报 `No space left on device`，先 `docker system prune -a -f` 清理 colima 磁盘 |
| Dify 上传文档超 15MB 被拒 | `UPLOAD_FILE_SIZE_LIMIT` 默认 15MB，且该变量不在 `dify/docker/.env`（仅在 shared.env.example 模板），运行态静默回落。在 `.env` 写 `UPLOAD_FILE_SIZE_LIMIT=100` 后 `docker compose up -d api worker` 重建；详见 [知识库上传文件大小上限](#知识库上传文件大小上限️-部署到新服务器必查) |
| HiAgent 页提示「当前域名无访问权限」 | HiAgent 智能体配置了网站访问白名单（WebSiteList）；到 HiAgent 控制台把当前访问域名（如 `http://localhost:5173`）加入 WebSDK 嵌入白名单，或清空白名单允许所有域名 |
| HiAgent 页一直加载/脚本加载失败 | 需能访问外网 `hia.volcenginepaas.com`；公司网络拦截时放开该域名，或检查 appKey 是否有效（appKey/baseUrl 在 [hiagent/index.vue](web/src/views/hiagent/index.vue) 顶部常量维护） |

## 智能问答交互（前端）

问答页 `web/src/views/chat/index.vue` 按聊天式设计稿实现，品牌色 `#6157FF`（紫蓝），设计令牌仅在本页 scoped 生效，不改全局主题。聊天卡片铺满后台内容区（去掉 `max-width` 居中约束，`height:100%` flex 撑满），头部与输入区固定、消息区 `.chat-scroll` 内部滚动；欢迎语垂直居中，提问后消息从顶部开始排列（首条问题在右上角），流式步骤/答案追加时自动滚动到底部：

- **头部助手身份卡**：紫色渐变圆形头像 + 名称「杰克百晓生」+ 在线状态点（空闲绿色 / 思考中橙色脉冲），右上角「重新对话」按钮清空消息回到欢迎态。
- **欢迎空状态**：居中标题「你好！有什么可以帮你的吗？」+ 副标题；Dify 未配置时显示黄色引导条。
- **消息气泡**：用户消息右侧（灰色圆形头像 + 浅灰气泡 + 时间戳），助手消息左侧（紫色渐变头像 + 浅紫气泡）。助手回答前先显示三个紫色跳动圆点（typing），随后实时展示 LangGraph 步骤（🔍 分类 → ✏️ 改写 → 📚 检索 → ⚖️ 判定 → ✍️ 生成）。
- **引用来源**：按文档去重（同一文档只显示一条），默认展示前 5 条可展开；答案/引用/反馈条都在气泡内。
- **消息操作**：用户消息 hover 显示「复制 / 编辑」（编辑把内容填回输入框），助手答案 hover 显示「复制」，图标仅 hover 时出现。
- **建议问题**：横向滚动胶囊按钮，hover 上浮变紫。
- **输入区**：圆角大容器（focus 时紫色 ring），上方工具条三个开关/选择器，下方无边框 textarea + 圆形发送按钮（有内容时变紫）：
  - **模型选择**：下拉只列出「系统配置」中已配置的可用模型；选中模型随请求 `model` 字段传给后端，覆盖系统默认模型（占位项「（未配置 LLM）」不覆盖）。
  - **深度思考**：开关，随 `deep_think` 传后端。开启后知识库召回 `top_k` 由 5 提升到 10，生成节点温度调高并追加「分步拆解、逐一引证、详尽回答」指令；助手消息 meta 前缀显示「深度思考 ·」。
  - **知识库检索**：开关 + 知识库多选弹层。开启时按勾选的知识库检索（`library_ids`，来自「知识应用 → 知识库」登记的 RAGFlow / DIFY 镜像，按平台分组展示），可全选/半选；关闭时传 `null` 走全部已启用的知识库。
- **流式**：`POST /api/v1/search/chat/stream`（SSE）逐节点推送 `step` / `answer_delta`（回答文本增量，逐字上屏）/ `final`（含 `usage` token 用量）/ `choice` / `choice_pause`（限时探索确认按钮，本轮无 `final`）/ `config_error` 事件；`ChatIn` 支持 `model` / `deep_think` / `history` / `session_id`（映射 DeerFlow thread，重新对话时刷新）/ `action`（限时探索续跑：`continue` 继续探索、`stop` 立即收尾，留空为新问题）参数（非流式 `/chat` 同步支持）。
- **下一步问题建议**：回答完成后，答案反馈条下方显示「你可能还想问」3 个追问胶囊（由模型生成，点击直接提问）。

**知识库（检索抽象层）**：「知识应用 → 知识库」（路由 `/apply/knowledge-libraries`，[knowledge-libraries/index.vue](web/src/views/knowledge-libraries/index.vue)）统一登记 **RAGFlow / DIFY 知识库镜像**，供智能体检索选库。仅登记 `platform + dataset_id` 引用（**只做镜像，不支持导入/解析新文档**，文档与解析仍在原平台维护），与「知识源管理」（推送路径定义）相互独立。后端 CRUD 见 [knowledge_library.py](services/kb-api/app/routes/knowledge_library.py)（`/api/v1/knowledge-libraries`，同平台同 dataset_id 不可重复，写操作限管理员），数据模型 `KnowledgeLibrary`（[models.py](services/kb-common/kb_common/models.py)，迁移 0025）。检索真相源切换：问答请求首选 `library_ids`（[search.py](services/kb-api/app/routes/search.py) `_resolve_retrieval_targets` 按 platform 解析为 dify/ragflow 两组 dataset，兼容旧 `knowledge_source_ids`/`dify_dataset_ids`；都不传时默认全部启用的知识库），sidecar 兜底（[agent_internal.py](services/kb-api/app/routes/agent_internal.py) `/internal/kb/retrieve`）同样优先抽象层、为空时回退旧知识源注册表；**检索策略由抽象层按 platform 内部决定**（Dify 走数据集自身检索配置含 rerank，RAGFlow 走引擎检索参数），提示词与工具描述统一为「知识库检索」，不再写死 Dify。

- **检索测试**：知识库页工具栏「检索测试」按钮（管理员）打开**双栏测试工作台**（参考终端检索测试台布局）——左栏为大输入卡（模式标签 + 知识库范围弹层多选，全选时显示「全部知识」+ 200 字检索词 + Enter/按钮执行）与**最近测试**（localStorage 仅保留 60 天，点击单条记录自动重新执行查询）；右栏为检索设置表格（检索模式/TopK/Score 阈值/向量权重，后两项仅 RagFlow 生效，默认 0.2/0.3 与智能问答链路一致）与测试结果（耗时、逐库状态 chips：命中 N 段/失败原因悬浮/已停用，命中分段卡片：排名徽标 + 所属库 + 文档名 + Score 分数条 + 正文 5 行截断可展开 + 字数/页码/分段 ID）。调用 `POST /api/v1/knowledge-libraries/retrieval-test` **逐库执行与智能问答一致的底层检索**，每库返回 ok/命中数/失败原因（如 RAGFlow `code 102` 无权数据集）。用于排查「RAGFlow 自带检索能搜到、智能问答搜不到」的断层——库级权限/配置问题一眼可见。

**项目文档库（知识应用 → 知识库 → 文档库页签，[DocumentLibraries.vue](web/src/views/knowledge-libraries/DocumentLibraries.vue)）**：项目自有文档库，上传 → 解析 → 分段 → 检索测试全链路**本地化，不依赖 RAGFlow**。

- **解析引擎**：本地 MinerU（mineru-kit V1 API，env `STRUCTURED_KIT_BASE_URL`，默认 `http://127.0.0.1:8010`）。pdf/docx/pptx/xlsx/html 走 MinerU 三步上传 + 异步解析 job（前端轮询 `POST /documents/{id}/refresh`，job 完成时下载 markdown 并落地分段）；**txt/md/csv 纯文本类型直通本地分段**（MinerU 不支持纯文本，原文即分段输入）。解析引擎实现 [mineru.py](services/kb-api/app/services/knowledge_engines/mineru.py)，路由 [managed_library.py](services/kb-api/app/routes/managed_library.py)（`/api/v1/document-libraries`，迁移 0035：`library_documents.engine_job_id` + `library_chunks` 表）。
- **分段规则全部保留**（RAGFlow 时代配置项不变）：`chunk_method=one` 整篇单段；`chunk_method=auto` **【自动】分层瀑布分段**（无大模型，三级逐级兜底：① 结构切分——fence 感知扫描 markdown 标题树切节，节内保留标题行，超长节续块补「标题路径」行便于溯源；② 递归长度修正——超长节按 段落（围栏代码块整体为一块）→ 原子行（连续表格行/图片行不拆）→ 句子（`delimiter`）→ 字符滑窗（重叠 1/8）逐级递归，目标长度 `chunk_token_num`，过短邻块同节内合并；③ 统计兜底——全文无标题时 TextTiling-lite：滑动窗口间隙评分（每间隙取左右各 2 句的字符 1/2-gram 余弦），有自然段时仅以段落边界为候选（避免窗口骑跨边界与段内噪声谷值），谷值深度 ≥ max(μ+σ/2, 0.1) 且间隙相似度 ≤ 0.5 且局部极小处判为主题边界，产出仍走②）；naive 等其他策略按 `delimiter` 切句 + `chunk_token_num` 滚动窗口聚合（单句不硬拆）；`overlap` **分段重叠度**——滚动聚合闭合分段时携带上一分段尾部总 Token ≤ overlap 的完整单元（句子/段落/行），相邻分段语义连续，0 不重叠、默认 25（超限自动放弃本次重叠，绝不产生重复分段）；**表格原子化**——HTML 表格（MinerU 复杂表格产物，单元格可能含句末标点）与 markdown 管道表格在切分前整体摘出为占位符、切分完成后还原：任何策略（含 naive 切句与字符滑窗兜底）都不会从表格中间打断（宁可整块超出也不切断），表格占位不进入相邻分段的重叠尾（整表不跨分段重复），分段内容完整保留表格结构、前端按 markdown/HTML 渲染为表格；**表格结束后强制分段**——表格作为分段结尾（表格前引文可同段），后续正文从新分段开始，下一分段即使有 overlap 也不回带表格内容；表格下标（表注）吸附：占位后首个非空行若为注释类短行（注：/备注/说明/表 N/Table N/* /①…，≤100 字符），并入表格块随表格同分段；存量被切碎的分段需「重新解析」按新规则重建；**文本预处理规则**（分段前执行，语义对齐 Dify `pre_processing_rules`）：`replace_whitespace` 把 3+ 连续换行折叠为 2 个换行、2+ 连续空格/制表符（含全角空格）折叠为单空格（保留换行结构，不影响标题识别）；`remove_urls_emails` 删除所有电子邮箱地址与 http(s) 裸 URL（markdown 链接/图片整体占位保护后还原，链接文字/图片引用不误删）；父子分段 `enable_children` + `children_delimiter`（子分段存 `parent_id`，仅参与检索命中，分段列表只展示父分段，仅 naive 生效）；`auto_keywords`/`auto_questions` 保留配置但本地解析不生成；`layout_recognize` 保留配置（MinerU 恒做版面识别）。分段器 [local_chunker.py](services/kb-api/app/services/knowledge_engines/local_chunker.py)（剔除 data:image 内联图防存储膨胀）。
- **分段管理**：点击文档名称在**门户页签独立打开分段详情页**（路由 `/apply/knowledge-libraries/:libId/documents/:docId`，[DocumentSegments.vue](web/src/views/knowledge-libraries/DocumentSegments.vue)，页签标题动态显示文档名）；本地 `library_chunks` 表 CRUD（**每张分段卡片右上角操作区**：向前插入分段/向后插入分段/编辑/删除/启用停用开关，开关直接作用于卡片不入编辑窗；插入按 `position` 整体后移腾位，接口 `ChunkIn.insert_before/insert_after` 二选一；关键词过滤分页，**命中分段正文内关键词以 `<mark>` 黄底高亮**（仅处理渲染后 HTML 的文本节点，不碰标签/属性，清空搜索即还原）），导出 zip 含原件 + 全量分段 JSON + manifest；重新解析会清除旧分段（含人工修改）并按库当前规则重建。**查看解析原文**：解析完成时原文（MinerU markdown / txt/md/csv 纯文本直通）持久化到 MinIO `{原件目录}/parsed.md`，分段页工具栏「查看解析原文」按钮**右侧面板**打开（与「查看源文件」互斥），JSON 内容格式化展示、markdown 按内容渲染（图片走同一鉴权代理），面板内「前往新页面查看」**新页签**打开独立页面（路由 `/apply/knowledge-libraries/:libId/documents/:docId/parsed-content`），接口 `GET /documents/{id}/parsed-content`（返回 `{content, format}`，format 由后端嗅探：合法 JSON 对象/数组 → json，否则 markdown）；历史文档无解析原文时提示「请重新解析后生成」。分段详情页工具栏「检索测试」按钮**新页签打开**文档级检索测试（路由 `/apply/knowledge-libraries/:libId/documents/:docId/retrieval-test`），知识库级同样新页签打开（路由 `/apply/knowledge-libraries/:libId/retrieval-test`）；两级共用公共组件 [RetrievalTestPanel.vue](web/src/components/common/RetrievalTestPanel.vue)（`scope=document` 时按当前文档过滤命中结果），解析原文面板与新页面共用渲染组件 [ParsedContentView.vue](web/src/components/common/ParsedContentView.vue)，检索方式点击弹出 [RetrievalSettingsDialog.vue](web/src/components/common/RetrievalSettingsDialog.vue) 配置检索方案（混合/向量/全文三选一，混合模式下配 Rerank 模型/TopK/Score 阈值）。
- **文档级检索状态**：文档列表「状态」列开关（启用/禁用，[managed_library.py](services/kb-api/app/routes/managed_library.py) `POST /documents/{id}/enabled`，迁移 0036：`library_documents.enabled`）。**禁用需二次确认**；禁用后该文档分段从检索通道摘除（检索测试/知识问答的本地包含匹配按 `enabled` 过滤，等效"删除检索索引"）；重新启用即恢复检索（分段数据保留在本地库，人工修改不丢，等效"重建索引"即时完成，无需重新解析）。解析中的文档不可切换。
- **文档级索引设置**：操作列「设置」按钮弹窗（[IndexSettingsDialog.vue](web/src/components/library/IndexSettingsDialog.vue) 弹窗壳 + **共用分段组件 [IndexSettingsPanel.vue](web/src/components/library/IndexSettingsPanel.vue)** + 策略转换 [index-settings.ts](web/src/components/library/index-settings.ts)）：分段策略卡片**点击内联展开**配置（已移除「系统/自定义」页签；自动/自定义/父子分段，知识库级另有按文件类型；【自动】映射 `chunk_method=auto`，走分段器内置分层瀑布，512/默认分隔符作为长度修正参数；【自定义】展开：分段方式 + 分段标识符多选（换行/2个换行/中文句号/中文叹号/中文问号/英文句号/英文叹号/英文问号/省略号，勾选「自定义」出现输入框输入自定义字符，序列化为分隔字符集合存 `delimiter`）+ 分段最大长度 + 分段重叠度（默认 25）+ 文本预处理规则两复选框（替换掉连续的空格、换行符和制表符 / 删除所有URL和电子邮箱地址，存 `replace_whitespace`/`remove_urls_emails`，解析分段前真实生效）；【父子分段】展开子分段标识符输入）+ 检索内容增强四开关（加入文件名/模型补充摘要/模型补充用户问题/模型补充图片描述，持久化暂不生效）。保存走 `PUT /documents/{id}/config`（迁移 0037：`library_documents.engine_config`，结构 `{processing, strategy, enhancements, type_rules}`）；解析时文档级 `processing` 逐键覆盖库级（空值不覆盖），当前分段不变。**确定时提供「保存后重新解析」解析开关（默认开启，弹窗底部）**：开启则保存配置后立即调用 `POST /documents/{id}/parse` 按新设置重新分段（清除已有分段及人工修改，tooltip 提示后果）；关闭则维持原行为（下次手动重新解析生效）。**知识库设置弹窗（编辑态）同样提供解析开关（默认关闭）**：开启后保存设置并对库内全部非解析中文档逐个触发重新解析（单个失败不阻断，Message 汇报 `成功/总数`）。
- **创建/编辑知识库共用分段组件**：创建知识库两步向导（基础信息 → 解析与分段）第二步**与文档设置共用同一份 IndexSettingsPanel**（scope=library 展示「按文件类型」策略；两步用 `v-show` 保持挂载，步骤往返不丢分段表单状态），向导内另保留解析引擎（MinerU）/向量模型/编辑态重新解析开关。库级设置随共用组件升级为**全量策略视图持久化**：`POST/PUT /document-libraries` 载荷在扁平 processing 字段外增加 `strategy`/`enhancements`/`type_rules`（与文档级 `DocumentConfigIn` 对齐），后端存 `engine_config = {processing, strategy, enhancements, type_rules}`，`lib_out` 返回同结构全量配置（旧库未存 strategy 时按 processing 推导：父子 → parent_child、auto → auto、否则 custom；旧扁平载荷仍兼容，strategy 缺省 auto）。未配置文档打开设置仍按库级 processing 推导（不继承库级 by_file_type 到文档视图）。
- **检索**：检索测试对文档库走本地包含匹配（命中子分段返回父分段内容）；智能问答外部检索兜底（`/internal/kb/retrieve`）**已排除文档库**（其 dataset_id 为本地标识，不参与 Dify/RAGFlow 通道）。
- **知识详情**：操作列「详情」按钮**右侧抽屉**（560px）展示四分区表单——基本信息（知识标题/知识ID/解析状态/来源系统/检索权限 开放·受限/创建时间/更新时间/训练时间/分段数量）、正文信息（文件卡：名称 + 大小 + 训练耗时）、标签信息（知识标签，无则「暂无配置」）、索引设置（分段策略/分段方式/分段最大长度/分段重叠度/文本预处理，文档级设置优先、回退库级）；底部「关闭/编辑」，编辑关闭抽屉并打开文档设置弹窗。操作列外显 3 项（详情/设置/更多），重新解析/停止解析/下载原文/删除收进「更多」下拉（危险操作保留二次确认）。
- **创建归属**：库列表展示创建人/创建时间（迁移 0038：`knowledge_libraries.creator`，创建时记录当前用户 username；存量库创建人显示 `-`）。迁移 0039 为并行分叉合并点（0038_library_creator + 0038_library_document_timestamps）。
- **旧 RAGFlow 文档**：保留原状态展示；原 PARSING 文档刷新后标记「解析引擎已切换为 MinerU，请重新解析」，重新解析即走新链路（pdf/docx/pptx/xlsx/html 经 MinerU，txt/md/csv 直通）。

## 智能体配置（DeerFlow 2.0 底座）

问答智能体以开源 [DeerFlow 2.0 Enhanced](https://github.com/stophobia/deerflow2.0-enhanced) harness 为 Agent 底座（Lead Agent 中间件链 + 子智能体 + 长期记忆 + sqlite 检查点，详见上方「架构」章节）；内置 LangGraph 工作流（Planner/Researcher/Reporter 思路：规划 → 检索 → 反思循环 → 报告）作为 sidecar 不可用时的回退与配置数据源，全部行为由配置驱动。配置入口在**侧边栏左下角「智能体配置」**（紫色图标，管理员可见，路由 `/agent-config`），配置以 JSON 存于 settings 表（key=`agent_config`），并通过 `/api/v1/agent/bootstrap` 下发给 DeerFlow sidecar 生成 `config.yaml`。

**后端工作流**（[workflow.py](services/kb-api/app/services/agent/workflow.py)）：

```
问题分类 → ├ 模糊 → 澄清 → END
          ├ 无关 → 拒答 → END
          ├ 闲聊问候（智能模式）→ 寒暄直答 → 追问建议 → END
          └ 业务问题 → 改写 → 任务规划(复杂/深度) → 知识检索
                 → 充分性判定 ┬ 充分 → 生成答案(Reporter)
                             ├ 不足且未超轮数 → 查询反思改写 → 回到检索（Researcher 反思循环）
                             └ 超轮数 → 深度探索
                 → 下一步问题建议(可选) → 保存上下文
```

**配置项**（`GET/PUT /api/v1/agent/config`，`GET /api/v1/agent/greeting`）：

| 分组 | 配置 | 说明 |
|------|------|------|
| 基础 | 模型 models | 参与调度的模型（最多 10），问答页下拉优先使用；为空用系统默认 LLM |
| 基础 | 知识库 retrieval_mode | `smart` 智能调用（闲聊直答不检索）/ `force` 强制调用（每问必检索） |
| 基础 | 对话开场白 | 开关 + 欢迎语 + 推荐问题（最多 6），问答页欢迎区读取 |
| 基础 | 下一步问题建议 | 回答后生成 3 个「你可能还想问」追问 |
| 高级 | 任务规划 planning_enabled | 复杂/深度问题先由 Planner（DeerFlow Todo 中间件）拆解计划 |
| 高级 | 子智能体协作 subagent_enabled | DeerFlow Lead Agent 将复杂问题并行派发 Sub-Agent 分头调研再汇总（更全面但更慢更耗 Token） |
| 高级 | 长期记忆 long_memory_enabled | 携带多轮对话（DeerFlow thread 检查点 + 摘要压缩），支持指代追问 |
| 高级 | 默认深度思考 deep_think_default | 新会话默认开启深度思考 |
| 超参 | temperature / top_p / max_tokens | 生成温度、核采样、最大输出 token |
| 超参 | top_k | 知识库召回条数（深度思考自动提升至 ≥10） |
| 超参 | max_retrieval_rounds | 检索反思轮数（1-4），资料不足时换角度重检 |

**人格与技能（SOUL.md / SKILL.md）**：「智能体配置 → 人格与技能」Tab 提供两个提示词编辑器，可在线修改 DeerFlow 运行时的**系统人格**（身份/职责/双源检索策略/作答规范）与**问答技能**（企业知识库问答工作流指令，含 name/description/version frontmatter）：

- 链路：前端 `GET/PUT /api/v1/agent/persona`、`/api/v1/agent/skill`（管理员）→ kb-api 代理（X-Internal-Token）→ DeerFlow sidecar `GET/POST /v1/persona`、`/v1/skill`（[qa_server.py](services/deerflow/backend/app/qa_server.py)）。
- **热生效**：保存后 sidecar 立即重写运行时文件并 `reset_client()` 重建 Agent，新对话即时生效；进行中的会话不受影响。
- **持久化**：自定义内容存于 `DEER_FLOW_HOME/persona.custom.md`、`skill.custom.md`，sidecar 重启/重载时优先使用自定义文件；默认模板内置于 qa_server.py（`SOUL_MD`/`SKILL_MD` 常量），点「恢复默认」即删除 custom 文件并恢复模板；SKILL.md 同步写入 `services/deerflow/skills/custom/enterprise-kb-qa/SKILL.md`。

前端配置页 `web/src/views/agent/config.vue` 仿智能体平台卡片式布局（基础设置/高级设置/人格与技能三个 Tab + 折叠卡片 + 开关/单选/滑块 + 等宽字体提示词编辑器）。

## Dify 知识库服务

本项目内置一份 Dify 源码（`dify/` 子目录，原版 1.16.1），用 Docker Compose 本地启动，作为智能问答 Agent 的知识库检索后端。

### 启动 Dify

```bash
cd dify/docker
# 首次：.env 已预配（INIT_PASSWORD=Dify@2026、EXPOSE_NGINX_PORT=8088）
docker compose up -d
```

启动后：
- Dify 控制台：http://127.0.0.1:8088 （首次用 `admin` / `Dify@2026` 登录设置）
- 内部服务（api:5001 / web:3000 / weaviate / postgres / redis / sandbox / plugin_daemon / agent_backend）在 compose 网络内互通，仅 nginx 8088 暴露到主机
- 镜像拉取受网络影响时，跑 `bash dify/docker/pull-images.sh` 单镜像重试续传

### 知识库上传文件大小上限（⚠️ 部署到新服务器必查）

Dify 文档上传大小由 `UPLOAD_FILE_SIZE_LIMIT` 控制（**单位 MB，内部默认 15**）。坑点：该变量只存在于模板 `dify/docker/envs/core-services/shared.env.example`，而容器实际加载的是 `dify/docker/.env`；若 `.env` 未显式写入，运行态会**静默回落到 15MB 默认值**（`docker exec docker-api-1 printenv UPLOAD_FILE_SIZE_LIMIT` 为空即中招）。

本仓库 `dify/docker/.env` 已显式配置：

```dotenv
UPLOAD_FILE_SIZE_LIMIT=100    # 知识库单文档上传上限 100MB
UPLOAD_FILE_BATCH_LIMIT=100   # 单次批量上传文件数上限 100
```

改后须 `docker compose up -d api worker` 重建容器生效；同时确认 nginx 请求体上限 `NGINX_CLIENT_MAX_BODY_SIZE`（docker-compose 默认 `100M`）≥ 该值，否则大文件会先被 nginx 以 413 拦掉。

> **部署提醒**：`dify/docker/.env` 被 gitignore、不进版本库，rsync/clone 到服务器时**不会带上**。每次在新服务器部署或重建 Dify 后，务必检查该文件里是否已写入 `UPLOAD_FILE_SIZE_LIMIT=100` 和 `UPLOAD_FILE_BATCH_LIMIT=100`，否则线上仍是默认的 15MB / 单批 5 个。

### 获取 Dify API Key

1. 登录 http://127.0.0.1:8088 → 「知识库」新建一个 Knowledge Base → 上传文档/导入内容
2. 进入该知识库 → 左侧「API」分页 → 复制右上角 **Dataset API Key**（形如 `{...}`）
3. 多个知识库可在同一 Dataset API Key 下管理（App/Console Key 也可，需有多数据集权限）

### 配置 Dify 模型供应商（语义检索必需）

Dify 知识库的**语义检索/混合检索**在查询时要用 Embedding 模型把问题向量化。若 Dify 未配置默认 Embedding 模型，检索接口返回 `400 Default model not found for text-embedding`（本项目客户端会自动降级为全文检索，不报错，但高质量索引的文档可能无命中）。

在 Dify 控制台（http://127.0.0.1:8088）：
1. 右上角「设置 → 模型供应商」配置一个 OpenAI 兼容的 Embedding 模型（如 OpenAI / 本地网关的 `text-embedding-3-small`、`bge-m3` 等）
2. 在「设置 → 模型供应商 → 默认模型」中将其设为系统默认 **Embedding 模型**（如用重排，再设默认 Rerank 模型）

> **检索重排（Rerank）**：智能问答检索各 Dify 知识库时，**优先使用该知识库在 Dify 控制台保存的检索设置**（检索方式、Rerank 模型、分数阈值均以知识库配置为准，仅放大 top_k）。在知识库的「检索设置」中启用 Rerank 模型后，检索结果即按重排分数返回；仅当知识库配置拉取失败时才回退到 `.env` 的 `dify_search_method` / `dify_reranking_enable`（默认关闭重排）。当召回内容只来自单一知识库时，直接采用 Dify 重排后的分数与顺序，不再做本地 RRF 融合；多知识库/本地知识库混合召回时仍走 RRF 融合。

### 接入到本项目

在「系统配置」页（路由 `/model`）：
1. 在「Dify 知识库」处「新增配置」：`API 端点` = `http://127.0.0.1:8088/v1`（**必须含端口与 `/v1`**）→ 保存
2. `API Key` = 上一步复制的 Dataset API Key（形如 `dataset-xxx`）→ 保存
3. 点「拉取列表」→ 数据集表格出现 → 点「填入全部 ID」→ 「数据集 ID」框自动填入 → 「启用」该配置（启用后同步为系统生效值）
4. 回到「智能问答」页 → 下拉框自动加载并全选数据集 → 提问即走 LangGraph Agent

> **智能问答同时依赖 LLM 与 Dify**：分类/改写/判定/生成都要调 LLM。若未配置 `LLM_API_KEY`，问答不再报 500，而是返回 `config_error=llm_not_configured`，前端在对话气泡中提示「请前往系统配置填写 LLM API Key」并带跳转按钮；Dify 地址配错时数据集列表会显示 `Dify 返回 5xx/4xx：…` 错误。

> 也可直接在仓库根 `.env` 写 `DIFY_BASE_URL` / `DIFY_API_KEY` / `DIFY_DATASET_IDS` 启动时注入。

### 知识流水线（RAG Pipeline）文档解析分流

流水线知识库按文件类型自动选择解析引擎（直接改 `workflows` 表图 JSON 实现，draft 与 published 全部生效）：

```text
File Upload (datasource)
  → 文件类型分流 (if-else: file.extension in [.pdf .ppt .pptx .doc .docx .png .jpg .jpeg])
      ├─ true  → MinerU (langgenius/mineru parse-file)
      └─ false → Dify Extractor (langgenius/dify_extractor)
  → 提取结果聚合 (variable-aggregator, 取两分支 .text)
  → Parent-child Chunker → Knowledge Base
```

- MinerU 引擎为本机 `services/mineru`（mineru-api 3.4.5，端口 2028），仅支持 pdf/docx/pptx/xlsx/图片；**不支持 .doc/.ppt/.xls 老格式**
- **.doc/.ppt/.xls 老格式**已在 `services/mineru/.venv/lib/python3.12/site-packages/mineru/cli/fast_api.py` 打补丁：上传时自动转对应 OOXML 格式再解析（`LEGACY_OFFICE_CONVERSIONS` + `_convert_legacy_office`）。转换器跨平台：**优先 LibreOffice（soffice），服务器部署必须 `apt-get install -y libreoffice-writer`**；macOS 本地开发对 .doc 回退 textutil。重装 venv / 升级 mineru 包后需重新应用补丁
- txt/md/xlsx/csv/html/json/yaml 等走 Dify Extractor（纯文本抽取，PDF 走 MinerU 获得更好的版面/表格/公式识别）

### 停止 / 清理

```bash
cd dify/docker
docker compose down            # 停止并删除容器（保留卷与镜像）
docker compose down -v        # 同时删除卷（清空 Dify 数据库与知识库，慎用）
```

## 知识中心

原「知识中心」页已拆分收编：钉钉知识列表迁入侧边栏「知识治理 → 入库审核」（`/govern/review`，含入库审核操作），本地上传知识列表页签已下线（后端 `GET /knowledge-center/documents`、回收站等接口保留）。

### 钉钉知识

实时拉取**钉钉开放平台**的企业知识库（Wiki）数据，非本地数据：

- **数据源与持久化**：`dingtalk_client` 调用 `v2.0/wiki/workspaces` + `v2.0/wiki/nodes` 遍历操作人可见的全部团队知识库；结果**持久化在 PostgreSQL `dingtalk_file_snapshots` 表**（只保留最新一份，payload 为文件 JSON，含创建人姓名）。列表接口只读快照，不调用钉钉，进程重启 / 容器重建 / 开发环境 `--reload` 后列表依旧可用。升级时若存在历史磁盘快照（`/tmp/kge_dingtalk_files.json`，路径可用 `KGE_DINGTALK_SNAPSHOT` 覆盖），首次访问会自动导入数据库。
- **刷新方式（仅手动）**：**只有点右上角「刷新」并确认**才触发后台全量遍历（会二次确认，提示约 25–30 分钟）；接口立即返回、前端按 12s 轮询并显示「后台同步中…」，期间列表继续展示当前快照；遍历成功后覆盖写入数据库快照并提示「钉钉知识快照已刷新」，失败则在表头显示告警（不弹成功提示）。没有任何定时/过期自动遍历——表头「数据更新于 HH:mm」即最近一次成功刷新的时间（悬停可看说明）。同一时刻只跑一个遍历（单飞）。
- **创建人姓名**：遍历结束时一次性经 `oapi.dingtalk.com/topapi/v2/user/get` 解析并写入快照，列表接口不再调用钉钉通讯录；仅对缺少姓名的历史快照做一次补解析并回写快照。
- **上级目录**：多层文件夹以 `/` 拼接完整路径（如 `/业务管理/AI与数字化团队/营销项目管理/M8系统`），知识库根目录下的文件显示 `/`。
- **元数据列**：文件名称（点击跳转钉钉文档）、来源知识库、上级目录、文件类型、文件大小、创建人、创建时间、最近更新、审核人。
- **入库审核**：操作列按钮即审核状态字段——未审核显示「审核」，点击弹窗选择 通过/待确认/待更正 并确认后，按钮文案变为所选状态（通过=绿/待确认=橙/待更正=红），再次点击状态字段可弹窗变更；新增「审核人」列记录执行审核动作的用户名（后端取当前登录用户，`PUT /knowledge-center/dingtalk/documents/review`）。审核记录按钉钉节点 ID 独立存储于 `dingtalk_file_reviews` 表（alembic 0044），快照刷新/覆盖不影响已审核结果；列表接口查询时按当前页 node_id 叠加审核状态与审核人。
- **过滤维度**：按知识库过滤（多选）、按创建人过滤（多选）、文件名搜索、上级目录路径搜索。
- **性能与容错**：钉钉 `wiki/nodes` 接口限流严格（实测 3 并发即批量 403），客户端采用**并发信号量（≤3）+ 全局最小请求间隔节流（0.4s/次，约 2.5 QPS）+ 403/429/5xx/超时指数退避重试**；全量约 4,500 个节点请求、**25–30 分钟**。明确无权限的目录自动跳过。智能问答的 `dingtalk_search` / `dingtalk_browse` 兜底工具读同一份快照（只读，不触发遍历）。
- **配置**：在「系统配置」页填写钉钉 AppKey / AppSecret / 操作人 UnionId（settings 表持久化，`sync_runtime_config` 每次调用前同步）；未配置时页面显示友好告警。
- **知识Owner通知（知识缺口）**：知识缺口页钉钉行操作列新增「通知」按钮（仅**快照存在 + 已维护 Owner + 目录无文档**的行可点，其余禁用并以 tooltip 说明原因）；点击弹窗预览正文「你即将通过钉钉发送私聊通知给知识Owner xxx：【库名 / 目录路径】该目录下的知识为空，请尽快补充，谢谢！」，确认后经 `POST /governance/gaps/dingtalk/notify` 走钉钉企业机器人单聊（`v1.0/robot/oToMessages/send`，sampleText）。前置条件：①「系统配置 → 钉钉设置」填写 **机器人 robotCode**（`dingtalk_robot_code`，取值见开放平台「应用详情 → 机器人」，与 AppKey 不同）；②应用开通「企业内机器人发送消息权限」与「通讯录个人信息读权限」；③ Owner 填写的是员工**姓名**（后端按姓名在通讯录精确匹配 userid，无精确匹配返回 400）。发送成功返回 `已通过钉钉私聊通知 {owner}`，错误分类：400 业务校验、502 钉钉 OpenAPI 网络/权限错误。

### 本地下线说明

「本地上传知识」页签（平台内上传文档列表 + 回收站/详情弹窗）已随知识中心收编下线，前端组件（KnowledgeManageTab / RecycleBinDialog / KnowledgeDetailDialog / TaskQueueBar / CenterDirectoryTree）与 `web/src/api/process.ts` 已删除；后端接口（文档列表、回收站、任务统计、`/api/v1/process/*` 加工接口）保留，上下文扩展接口仍被智能问答调用。

## 知识采集（collection 模块）

侧边栏「知识采集」（路由 `/collection`）含六个页签，覆盖从钉钉单文件同步到 Dify 知识库批量入库，再到定时增量同步与运行监控的完整采集动线：

- **从钉钉同步**：**按目录查询**（避免全量遍历超时）——依次选择钉钉知识库 + 文件夹（级联懒加载子目录，任意层级可选）后点「查询」，仅加载该目录的**直接子文档**；首次进入未选择时列表为空。实时数据 + 浏览器内存缓存（5 分钟 TTL，不持久化），「强制刷新」绕过缓存重新拉取。过滤文件名/分页为前端本地进行。后端新增轻量接口（[knowledge_center.py](services/kb-api/app/routes/knowledge_center.py)）：`GET /knowledge-center/dingtalk/workspaces`（实时列团队知识库，含根节点 ID）、`GET /knowledge-center/dingtalk/nodes?parent_node_id=`（实时列某父节点直接子节点），均为单次钉钉 API 调用（约 1.5s）；勾选文档同步到 Dify 沿用 `POST /api/v1/dify/datasets/{id}/sync-dingtalk`（请求体传 `node_id`，后端用 `dingtalk_client.download_document` 走 `queryDentryId → downloadInfos/query → OSS 直链` 下载原文件；**注意**：钉钉 wiki 节点的 `url` 字段是 alidocs 在线预览页，直接下载会得到 HTML 导致 Dify 解析失败，且 OSS 返回的文件名是无扩展名哈希串，上传时须用前端传入的带扩展名原始文件名）。**源文档直传（不做本地转换）**：源文件先备份对象存储（Minio `raw-docs/dingtalk-sync/{node_id}/`），≤15MB 按目标库类型选择 `create-by-file` 或 `pipeline/run` 以源文档上传；>15MB 时 Dify 硬性限制无法直传，明确报错并返回源文件 OSS key。**禁止经 MinerU/文本抽取转换后上传**（历史转换产物内容失真：表格/图片/排版丢失，仅剩分页文本流）。在线文档（adoc/axls/able 等）无 OSS 原文件，仍走钉钉官方导出接口得 docx/xlsx/pdf 后源文件上传。
- **上传到 Dify 知识库** / **上传前 AI 预检** / **缺口与征集**：单文件与批量上传、AI 预检与缺口看板（详见各页签内说明）。
- **同步源管理**（后端 `app/routes/sync_route.py`，`/api/v1/sync/sources`）：配置钉钉知识库目录 → Dify 数据集的映射关系，每个同步源自带独立 `cron` 定时表达式（默认 `0 2 * * *` 每日凌晨 2 点）。支持新增/编辑/启停/删除、**同步列表**（原「预演」，dry-run 只比对不写入；**点击立即弹窗、在弹窗内等待加载**——行内按钮不转圈，表格区域显示 loading 遮罩，加载失败自动收起弹窗；结果弹窗前端分页每页 20 条；**全量展示**该目录下所有文档——未同步过标「新增」、上一轮已同步标「更新」，另有「跳过/删除」，每篇带「参与同步」开关，开关打开的文档同步时一律重新上传、不做内容 hash 跳过；**性能**：目录树快照服务端缓存 10 分钟——内存 + `/tmp/kge_sync_walk_cache.json` 磁盘快照（重启热身）命中秒回，未命中时兄弟目录并发遍历（≤3 并发 + 全局 0.4s 节流防钉钉限流）；弹窗内「刷新列表」传 `refresh=true` 强制重遍历（同样弹窗内等待）并保留已调开关；真实同步始终绕过缓存直读钉钉最新目录；数据集 runtime 模式缓存 10 分钟）、**立即同步**、连接测试。表格含**目标知识库类型**列（「知识流水线」/「普通知识库」标签，来自数据集 runtime 模式，服务端缓存 10 分钟；查询失败或 RAGFlow 不可用时显示 —）。操作列仅保留「立即同步 / 同步列表 / 启停开关 / 更多▼」（编辑与删除收进「更多」下拉）。**文档数 = 该源映射表中 status=synced 的数量**（多源可共享同一 Dify 数据集且数据集可能含手动上传文档，不能用数据集全库文档数作为单源口径）。表单中**目录必选**（点选目录树节点，不再默认整库同步；「已选目录」只读回显所点选路径）；目录树在首次选择知识库时自动拉取完整目录并持久化到 localStorage（`kge:sync_dir_tree:{workspace_id}`），再次打开弹窗直接用缓存不请求，搜索框旁「刷新」按钮强制重新拉取。同步源保存后由 `app/services/sync/scheduler.py`（APScheduler BackgroundScheduler）按 cron 注册任务，增删改自动 `reload_sync_jobs`。
- **运行监控**（合并自源项目 Monitor + Logs 单页三区块）：① **失败清单**（待处理，可单项/全部重试）；② **同步历史**（运行记录表：触发方式/耗时/新增·更新·删除·跳过·失败计数/结果状态）；③ **运行日志**（按级别 info/warn/error、关键字、时间窗筛选）。

### 钉钉知识库 → Dify 定时增量同步（合并自 DingDingKonwledgePipeline）

并入平台后保留的核心能力，相关代码分布：

- **数据模型**（迁移 `alembic/versions/0011_add_sync_tables.py`）：`sync_sources`（含 `cron` 字段，不保留独立 jobs 表）、`sync_runs`、`sync_document_mappings`、`sync_failures`、`sync_logs`，模型定义见 [kb_common/models.py](services/kb-common/kb_common/models.py)。
- **同步引擎**（[services/sync/engine.py](services/kb-api/app/services/sync/engine.py)）：钉钉目录树遍历（并发 ≤3 + 0.4s 节流；真实同步 `use_cache=False` 直读最新目录，预演走 10 分钟快照缓存）→ 下载/导出（ALIDOC 与 .able 经 dws CLI）→ **源文档备份对象存储**（Minio `raw-docs/dingtalk-sync/{node_id}/`）→ **源文档直传 Dify**（预演开关打开的文档一律重新同步：已同步过走更新、未同步过新建，**不做内容 hash/指纹跳过**；仅「预演关闭」或扩展名在跳过列表才跳过；`delete_policy=sync` 时同步删除已不存在的 Dify 文档）。**上传前不做任何本地转换**（无 MinerU/文本抽取——历史转换产物内容失真，已废弃）；仅受 Dify 15MB 单文件硬限制，超限报错。同步版 httpx + 同步 ORM，跑在 `ThreadPoolExecutor` 后台线程不阻塞事件循环。
- **知识流水线数据集分流**（[dify_sync_client.py](services/kb-api/app/services/sync/dify_sync_client.py)）：目标 Dify 知识库为 **RAG Pipeline 模式**（`runtime_mode=rag_pipeline`）时，`create-by-file` 会派发普通索引任务导致「No subchunk segmentation found in rules」失败、且控制台「处理/预览」报 `PublishedWorkflowRunPayload` 校验错误——此类数据集自动改走 `POST /datasets/{id}/pipeline/run`（上传暂存文件 → 阻塞运行已发布流水线 → 用响应中的 document id/batch 复用 `wait_indexing` 轮询）；更新场景因该接口不支持 `original_document_id`，先创建新文档并确认索引成功，再删除旧文档；失败时保留旧文档与原映射。流水线运行依赖 dify 镜像补丁 `base_app_generator.py`（必填变量缺失回退默认值）+ `variables_manager.py`（`default_value → default` 字段映射），见 [dify/docker/patches/README.md](dify/docker/patches/README.md)。
- **运行兜底与中断语义**（[runtime.py](services/kb-api/app/services/sync/runtime.py)）：单次同步硬超时 `sync_run_timeout_seconds`（默认 **28800 秒 = 8 小时**，超时强杀进程组）；超时/进程崩溃/孤儿收编时保留已完成的同步成果——有成功项则标 `partial`（部分成功，WARNING 日志），仅零成功才标 `failed`（ERROR），日志带「成功 N 个，失败 N 个」计数（与引擎正常路径的 partial 判定一致）。前端「同步源管理」与「同步工作台」对运行中任务每 3 秒轮询，全部结束后自动停止轮询，按钮由「同步中」恢复为「立即同步」可再次触发。
- **配置读取**：钉钉 AppKey/Secret/操作人、Dify base_url/api_key 复用「系统配置」页；同步参数（`sync_max_depth`、`sync_export_format`、`sync_skip_extensions`、`sync_dify_wait_indexing` 等）与 `dws_bin`/`dws_config_dir` 从 [kb_common/config.py](services/kb-common/kb_common/config.py) 读取，可用 `.env` 覆盖。
- **依赖**：kb-api 新增 `apscheduler`（cron 调度）与 `psycopg[binary]`（同步引擎的同步 DB 会话，连接同一套库；平台主 ORM 仍为 asyncpg）。
- **认证**：沿用平台 `require_role("super_admin","admin","editor")`，不保留源项目的 admin/password 哈希登录。
- **不保留**：独立 SyncJob 表（cron 合并到 sync_sources）、独立 Settings/Login/Dashboard 页、告警 webhook（alert.py）。

## 知识加工（process 模块）

侧边栏「知识加工」（父级路由 `/process`，重定向到 `/process/engine`）为二级菜单：

- **入库审核**（`/govern/review`，原「知识打标」页 `/process/tagging` 迁入知识治理分组并更名）：页面无页内签，仅保留钉钉知识列表（列表内完成入库审核：通过/待确认/待更正 + 审核人，见上文「钉钉知识」节）。原 加工总览/知识图谱/标签库/加工说明 页签及其前端代码已删除；AI 打标/摘要/关系构建仅保留后端能力（`/api/v1/process/*`），上下文扩展接口仍被智能问答与 DeerFlow 调用。
- **解析引擎**（`/process/engine`，双页签）：
  - **自定义解析**（[ParserCustomTab.vue](web/src/views/governance/components/ParserCustomTab.vue)）：**双引擎接入 + 统一契约**，页面交互（上传/队列/结果预览）完全复用，顶部「本地服务 / 云端 SaaS」切换并持久化 localStorage。**调用方只认一套契约**（[mineru_contract.py](services/kb-api/app/routes/mineru_contract.py) Pydantic schema，kb-api `/docs` 可查 10 个显式类型化端点）：三步上传（`POST /v1/uploads` → `PUT content` → `complete` 得 `file_id`）→ 建任务 `POST /v1/parse/jobs` → 2s 轮询 → `GET /v1/files/{id}/content` 拉产物。**引擎差异按能力发现封装**：`GET /v1/health` 返回 `capabilities`（engine / tiers / output_formats / cancelable / page_range / max_file_mb），调用方按能力适配而非按引擎适配；**档位统一语义三级** speed/balanced/quality（本地映射 flash/standard/advanced、云端映射 pipeline/vlm，响应保留 `raw_tier` 便于排查）；**产物统一 markdown + structured_json**（本地 middle.json 与云端 content_list.json 归入同一槽位）。引擎选择由 `X-Mineru-Base` 头指定：**本地服务**透传 MinerU V1 API（默认 `http://127.0.0.1:8010`，无鉴权，地址可配，代理层做档位/产物归一化）；**云端 SaaS** 由适配器（[mineru_saas.py](services/kb-api/app/routes/mineru_saas.py)）翻译为 V4 契约（`file-urls/batch` 预签名上传 → `extract-results/batch` 轮询 → 结果 zip 解包，Key 复用模型配置页 `mineru_api_key` 脱敏配置，前端不经手密钥；云端 `cancelable=false`，取消任务返回 409；任务注册表为进程内临时态，kb-api 重启清空）。显式路由未覆盖的本地 kit 私有端点由 catch-all 兜底透传，云端一律 404。解析队列：任务表含文档名称、类型、**产物ID** 列（一键复制）、历史任务自动补拉、状态/名称筛选、统计汇总、取消任务；点「查看」自动切回解析测试页签。结果预览 **markdown + structured_json 双视图**（marked 渲染 / 语法高亮），支持复制与下载：云端模式 markdown 视图下载**原始结果 zip**（`{名称}_markdown.zip`，内含 full.md + images/ 文件夹 + content_list.json，解压即为完整可读内容；本地模式 markdown 图片 base64 自包含，直接下 `.md`），JSON 视图下载 `_structured.json`；base64 内联图缩略展示（≤220px）；**zip 内相对路径图片真实渲染**（云端模式构造 `batch::idx::images/x.jpg` 经产物接口拉取 blob，懒加载+缓存+失败占位，宽度占满内容区；本地 kit 无成员寻址能力维持占位提示）。
  - **MinerU WebUI**：iframe 嵌入官方 WebUI（同源部署直连）。
  - **API 文档入口**（页签行最右侧，均新窗口打开）：【API 调用说明】打开文档页 [mineru-api-docs.html](web/public/mineru-api-docs.html)——基于 **Docsify**（组件 vendor 在 `web/public/vendor/docsify/`，内网不依赖 CDN）渲染 [mineru-api-docs.md](web/public/mineru-api-docs.md)，左侧边栏目录 + 全文搜索 + 代码一键复制。内容对应 **kb-api 统一契约**（`mineru_route.py` + `mineru_contract.py`）：双引擎（本地 Kit / 云端 SaaS）接入对照与 capabilities 能力发现、登录换 JWT、**全端点逐个配 curl + Python（requests）双语言调用示例**、page_range 语法、端到端 curl / Python 完整脚本、前端 axios 集成、错误排查；页内通过 `{{MINERU_BASE}}`/`{{SWAGGER_URL}}` 占位符自动跟随配置的引擎 Base URL。【OpenAPI】打开引擎 Swagger 调试台（`{Base}/docs`）。侧边栏左下角用户菜单同名入口「API 调用说明」（关于我下方，新浏览器页签打开）指向同一文档页，跳转逻辑共用 [api-docs.ts](web/src/utils/api-docs.ts)；后续新增其他 OpenAPI 服务在该工具与菜单中追加。
  - **CORS 代理**：浏览器直连 MinerU 会被 CORS 拦截（服务不带跨域头），全部请求经 kb-api 同源透传代理（[mineru_route.py](services/kb-api/app/routes/mineru_route.py)）`/api/v1/mineru/*`，目标地址由 `X-Mineru-Base` 请求头指定，仅 super_admin/admin 可用。

### 核心能力

- **AI 打标**：对 Dify 文档的分块内容调用 LLM，生成 3-6 个主题标签、3-8 个检索关键词，并识别文档类型（制度规范/技术文档/产品手册/FAQ 等）。标签统一写入 `knowledge_tags` 标签库，按使用次数统计。
- **摘要生成**：生成 200 字以内文档摘要，概括核心内容与适用场景。
- **知识关系构建**：对数据集中已加工文档两两分析，识别引用/相似主题/因果/包含/并列/前置知识/依赖/对比等语义关系，构建知识图谱（`knowledge_relations` 表，双向存储）。
- **上下文工程**：智能问答检索命中后，自动附加命中文档的摘要/标签（`internal/kb/retrieve` 富化），并提供 `knowledge_context_expand` 工具供 DeerFlow 智能体主动扩展关联文档，实现深度探索。

### 数据模型（迁移 `0014_add_knowledge_processing.py`）

| 表 | 用途 |
| --- | --- |
| `processed_documents` | 文档加工记录：dataset_id/document_id/name/tags/summary/keywords/doc_type/process_status |
| `knowledge_relations` | 知识关系：source_doc_id→target_doc_id，relation_type/weight/description |
| `knowledge_tags` | 标签库：name/color/count |

### 关键接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/process/datasets/{dataset_id}/documents` | 文档列表（合并 Dify 文档 + 加工状态） |
| POST | `/api/v1/process/datasets/{dataset_id}/documents/{document_id}/enhance` | 单文档 AI 打标+摘要 |
| POST | `/api/v1/process/datasets/{dataset_id}/enhance-all` | 批量加工 |
| POST | `/api/v1/process/datasets/{dataset_id}/build-relations` | 构建知识关系 |
| GET | `/api/v1/process/datasets/{dataset_id}/graph` | 知识图谱（节点+边） |
| GET | `/api/v1/process/documents/{document_id}/relations` | 文档关联文档 |
| GET | `/api/v1/process/context/expand` | 上下文扩展（摘要+关联+标签） |
| GET | `/api/v1/process/tags` | 标签库 |
| POST | `/api/v1/internal/process/context-expand` | 内部上下文扩展（DeerFlow 工具调用） |

> 实现：服务层 [services/processing.py](services/kb-api/app/services/processing.py)，路由 [routes/process_route.py](services/kb-api/app/routes/process_route.py)，Dify 文档/分块读取见 [dify_client.py](services/kb-common/kb_common/clients/dify_client.py) 新增 `list_documents`/`get_document_segments`。

## 知识运营（原运营看板）

侧边栏「知识运营」（路由 `/operate`）含三个页签：

- **运营看板**（后端 `app/routes/operate.py`）：知识存储总量/知识数量/热门知识 Top20 的定时统计与持久化。
- **问答明细**（后端 `app/routes/qa_route.py`，`GET /api/v1/qa/details`）：真实问答流水，块状列表展示用户、问题、回答链路（分类→改写→检索→判定→生成 steps）、召回片段（Score 进度条/文档名/片段内容/片段ID）、点赞/点踩反馈标签；支持按检索时间、检索内容（问题/回答关键词）、用户、反馈类型多维过滤与分页。数据来源：`chat_messages` 表，问答完成时由前端持久化 `detail`（链路 steps + 召回片段快照 + 模型）。**召回片段记录全量召回**（`detail.retrieval` 来自 `final.retrieval_all`，含未被答案引用的分段，每条 `cited` 标记是否被引用）——标题显示「召回 N 条，其中被答案引用 M 条」，逐条带「已引用/未引用」标签，可完整回溯过程中召回了哪些分段（旧数据无 `cited` 字段，仅含最终引用，视为全部已引用）。
- **知识纠错**（`GET/PUT /api/v1/qa/corrections`）：用户在问答页提交的纠错工单（`qa_feedbacks` 表，feedback_type=correct，错误类型含内容错误/知识重复/知识过期/知识难理解/知识不完整/知识模板错误/其它）；支持按知识标题、错误类型、状态过滤，管理员可处理（处理人自动记录、处理备注、状态流转 待处理→处理中→已解决/已关闭）。**知识标题可点击跳转原文**：钉钉文档（alidocs.dingtalk.com 链接）跳转钉钉知识库对应文档，Dify/本地文档跳转预览页；无链接的历史工单显示纯文本。

问答页反馈按钮（👍有帮助 / 👎纠错 / ❓没找到想要的）已全部接入真实接口（`POST /api/v1/qa/feedback`），点赞/没找到直接记录。**引用来源旁的「👎 纠错」按钮**可针对单条知识纠错：点击后自动带入该条知识的名称与链接（`knowledge_title`/`knowledge_url`，存入 `qa_feedbacks`，alembic 0010），弹窗中可直接打开原文链接核对；反馈栏的通用纠错按钮不带具体链接。引用来源的文档名本身也可点击（有外链时新窗口打开原文）。

### 运营看板指标口径

**统计机制与持久化（流水表）**

- **定时统计**：知识存储总量、知识数量（含分布）、热门知识 Top20 由 kb-api 内置调度器**每日 0 点自动统计**；也可点「刷新」按钮手动触发。统计流程：存储（秒级）→ 知识库全量遍历（约 30 分钟）→ 热门逐文档统计（约 30 分钟）。
- **持久化**：每个指标统计完成即写入流水表 `operate_metric_records`（alembic 0006，metric_key / metric_date / value(JSON) / trigger(daily|manual) / created_at），保留 180 天。
- **展示**：接口取各指标**最新一条流水**返回（内存快照镜像）；服务重启后自动从流水表恢复；若当天无记录（如 0 点时服务重启错过定时），启动后自动补统计。
- 知识召回数 / 调用量仍为「即将发布」，待知识采集流程打通、Dify 文档关联钉钉文档 ID 后基于 `usage_logs` 统计。

| 指标 | 数据来源 | 口径说明 |
| --- | --- | --- |
| **知识存储总量** | 优先钉钉企业存储 API `GET /v1.0/storage/orgs/{corpId}`；权限不足时自动降级**钉钉官方 CLI**（`tools/dws/dws`，`drive quota apps`） | 与钉钉知识后台口径**一致**：全应用存储用量汇总（钉盘 2.4TB + 钉钉文档 1.7TB + 会议 1.4TB + AI 听记等 19 个应用，当前约 **6.7TB**）。API 需应用开通「企业存储企业读权限 `Storage.Org.Read`」；CLI 走 `dws auth login` 扫码的**用户 OAuth 授权**，与应用权限相互独立，结果缓存 10 分钟。两条路都失败时回退为"仅文件大小"（约 0.8TB，在线文档 size=0 不可见） |
| **知识数量** | 遍历 `v2.0/wiki/nodes` 统计团队知识库文件节点 | 后台异步全量遍历 + 内存缓存（TTL 1 小时），首次约 25–30 分钟，前端 20 秒轮询。当前全量约 1.6 万个文件节点 |
| **企业知识数量分布** | 同上，按知识库聚合 | echarts 环形饼图 |
| 知识召回数 / 调用量 | 暂不取数 | 待知识采集流程打通、Dify 文档关联钉钉文档 ID 后统计，显示「即将发布」 |
| **热门知识 Top20** | 逐文档调用钉钉 CLI `dws drive stats`（阅读/访问/编辑/评论统计） | 按**访问次数**倒序（相同按阅读数），只统计知识文档（上传 DOCUMENT + 在线 ALIDOC，约 6,700 个）。钉钉无企业级排行接口，采用后台并发扫描（并发 3，约 30 分钟），结果缓存 24 小时；扫描期间展示已统计部分，面板显示进度 `统计中 x/y`，文档名可点击跳转钉钉原文 |
| 问答准确率 / 活跃知识占比 / 评测集通过率 / Owner 响应及时率 / 健康度分布 / Owner 贡献榜 | 模拟数据 | 均标注「示例数据」，后续有真实逻辑再替换 |

> **存储总量为什么与钉钉后台（几个 TB）对不上**：遍历只能累加**上传类文件**的 `size`（文档/图片/视频/音频/压缩包等，约 0.8TB）；**钉钉在线文档**（`.adoc`/`.able`/`.axls`/`.amind` 及快捷方式 `.dlink`/`.hlink`，约 260+ 个节点）节点接口返回 `size=0`，且个人钉盘/群文件不在团队知识库（Wiki）遍历范围内。这些只有企业存储接口能覆盖，因此准确的存储总量**必须开通 `Storage.Org.Read` 权限**，开通后无需改代码即自动显示真实值。
>
> **钉钉官方 CLI（dws）兜底**：`tools/dws/dws` 为钉钉开源 CLI（v1.0.61，darwin-arm64，Apache-2.0）。已配置 `dws auth login` 用户扫码授权（杰克科技，refresh token 约 30 天有效，过期后重新扫码即可）。后端 `get_org_storage_used()` 在存储 API 权限不足时自动调用 `dws drive quota apps`（分页汇总 19 个应用用量，TTL 10 分钟缓存），无需依赖应用权限。CLI 路径可用环境变量 `DWS_BIN` 覆盖；服务器/容器部署时需用 `dws auth export/import` 迁移凭证并以 `DWS_DISABLE_KEYCHAIN=1` 运行。

## 项目结构

```
knowledge-governance-expert/
├── docs/ui-guidelines.md        # UI/UED 设计规范（所有界面改动强制遵循，含交付自检清单）
├── docker-compose.app.yml       # 应用容器（kb-api/faq-service/kb-worker）
├── tools/dws/                   # 钉钉官方 CLI（dws），存储总量兜底查询（用户 OAuth 授权）
├── .env / .env.example          # 开发模式 env（含 Dify 配置）
├── .env.docker                  # Docker 全量模式 env
├── dify/                        # 内置 Dify 源码 + docker compose（知识库后端）
│   └── docker/
│       ├── .env                 # Dify 启动配置（INIT_PASSWORD / EXPOSE_NGINX_PORT=8088）
│       ├── docker-compose.yaml  # 完整 Dify 服务栈
│       └── pull-images.sh       # 单镜像重试拉取脚本（国内网络续传）
├── scripts/
│   ├── smoke_test.sh            # 端到端冒烟（覆盖 RAG + FAQ + API Key + 设置）
│   └── seed_admin.py            # 种子 admin 用户
├── services/
│   ├── kb-common/               # 共享：models/clients/rag/auth/config（含 dify_client）
│   ├── kb-api/                  # FastAPI + Celery
│   │   └── app/services/agent/  # 企业证据问答（enterprise/retrieval/DWS）+ 旧版兼容 Agent
│   │   └── app/services/sync/   # 钉钉知识库 → Dify 定时增量同步（engine/scheduler/clients）
│   └── faq-service/             # FAQ KB + 精准匹配（FastAPI）
└── web/                         # Vue3 + Element Plus 前端（七大治理模块）
    ├── public/about.html        # 「关于我」产品介绍页（侧边栏左下角用户菜单新页签打开；同菜单「API 调用说明」打开解析引擎 API 调用文档页，见 src/utils/api-docs.ts）
    └── src/views/
        ├── chat/                # 智能问答（Dify 数据集多选 + Agent）
        ├── hiagent/             # HiAgent智能问答（火山 HiAgent WebSDK iframe 嵌入）
        ├── governance/          # 知识治理（分组：知识缺口 /govern/gaps——原「知识治理」页，含知识缺口+治理标准页签、model.vue 接入配置；入库审核 /govern/review——原「知识打标」页迁入更名，钉钉知识列表操作列即审核状态字段：点击弹窗选择 通过/待确认/待更正 确认后文案变为所选状态、可再次点击变更，审核人列记录操作用户名，审核记录存 dingtalk_file_reviews 表按节点ID独立存储、快照刷新不覆盖；治理标准页实时读取钉钉多维表《杰克知识管理规范》，需应用开通 Notable.Base.Read.All 权限；知识缺口页知识库过滤选项取自知识源管理中已启用的钉钉知识库，选中钉钉知识库后查询读取 dingtalk_folder_stats 快照表（文件夹直属文档数=在线文档+本地上传文件（.dlink 文件夹快捷方式计为其所在文件夹的一个文档，不作为目录遍历——其子节点 API 无法列出）；「文件夹数量」列=该目录直属子文件夹数，不含目录本身与孙级——钉钉行由快照路径树推导、本地行走 parent_id，CSV 导出同口径）；筛选项：知识库、Owner、有无文档状态，以及「文档数量 / 文件夹数量」区间下拉（0 / 1-9 / 10-99 / 100+，按直属数量过滤，查询与 CSV 导出同口径，变更后由「查询」按钮触发），「刷新数据」按钮触发后台全量遍历并覆盖写入快照（几万文档的大库需数分钟，前端轮询进度，刷新保留已维护的 Owner）；知识Owner 支持行内编辑弹窗与批量导入（CSV/XLSX，按目录ID或知识库＋目录路径匹配，本地目录与钉钉文件夹均可），钉钉行另提供「通知」按钮——经钉钉企业机器人向 Owner 发单聊催补提醒（无文档行可点，正文预览见「知识Owner通知」说明）；新增钉钉知识源时自动触发该库快照预热，知识源列表显示「目录获取中」状态；知识采集的同步任务弹窗「目录选择」优先读取该快照表秒开（GET /knowledge-center/knowledge-sources/{id}/dingtalk-folder-snapshot，按路径树还原目录，快照缺失时自动回退实时遍历））
        └── settings/            # 兼容旧路由的模型配置页（隐藏）
```

## 企业问答增强与准确率验收

智能问答采用统一的企业知识检索、DWS 钉钉补查、原文摘录校验与语义核验链路。配置方式、权限边界、标签/摘要/图谱与记忆设计，以及超过 90% 准确率的专家题集验收方法见 [企业问答说明](docs/qa-evaluation/README.md)。实际验证范围见 [验证记录](docs/qa-evaluation/VALIDATION.md)。

### 问答输出与过程展示

问答页按实际工具调用显示处理、检索、读取、上下文展开及失败/取消状态。内部会话摘要与正式答案分开；正文在本轮主模型最终结果确认后展示，过程继续实时更新。有引用的答案标注“附有参考来源”，不再将提示词自检描述为事实核验。历史答案不会自动改写。实现及验收见 [问答输出隔离说明](docs/features/qa-output-isolation-plan.md)。
