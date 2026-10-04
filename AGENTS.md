# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Build & Run Commands

### Local development (recommended)

```bash
# 1. Start Docker infrastructure (run once; lives in separate docker-compose)
docker compose -f ~/Documents/03_Resource/开发环境/docker-compose.yml up -d

# 2. Configure .env at repo root (see README for full env template)

# 3. Install Python deps per service
cd services/kb-common && uv sync
cd services/kb-api && uv sync
cd services/faq-service && uv sync

# 4. Init DB + seed admin user
cd services/kb-common && uv run alembic -c ../../alembic.ini upgrade head
cd services/kb-common && uv run python ../../scripts/seed_admin.py
# → admin / admin123

# 5. Start 3 application processes (separate terminals)
cd services/kb-api && uv run uvicorn app.main:app --reload --port 8000
cd services/kb-api && uv run celery -A app.worker worker -Q ingestion --concurrency=2 -l info
cd services/faq-service && uv run uvicorn app.main:app --reload --port 8004

# 6. Start frontend
cd web && pnpm install && pnpm dev   # http://localhost:3000 or :5173
```

### Docker all-in-one mode

```bash
# Prerequisite: infrastructure must be running (step 1 above)
docker compose -f docker-compose.app.yml up -d --build
# Cleanup app containers only (infrastructure stays):
docker compose -f docker-compose.app.yml down
```

> Models are all external HTTP APIs (embedding/rerank/LLM); no local model loading, no extra colima memory needed.

### Frontend

```bash
cd web
pnpm dev          # dev server with HMR
pnpm build        # type-check + production build
pnpm type-check   # vue-tsc --noEmit
pnpm test:run     # vitest run
```

### Smoke test

```bash
./scripts/smoke_test.sh
# KEEP_SERVICES=1 → keep procs after run
# EXTERNAL_SERVICES=1 → services already running, skip launch
```

## Architecture

**MVP**: 3 application processes + 5 Docker infrastructure containers, sharing one PostgreSQL database.

```
Web (Vue 3) :5173  ──/api/v1──►  kb-api :8000       (FastAPI, models external)
                      /api/v1/faq ► faq-service :8004  (FastAPI)

kb-worker  (Celery, queue: ingestion)
  └─ MinerU parse → chunk → embed → ES index

Docker infrastructure (dev-services compose, external dev-network):
  PostgreSQL :5432  Redis :6379  Elasticsearch :9200  MinIO :9000  kkFileView :8012
```

### Service Map

| Process | Port | Stack | Responsibilities |
|---------|------|-------|-----------------|
| **kb-api** | 8000 | FastAPI + Celery app | Auth (JWT + API Key), user CRUD, document KB CRUD, directory tree, document upload, search (hybrid kNN+BM25+RRF+rerank), enterprise QA via DeerFlow sidecar (search routes → deerflow_runner → services/deerflow qa_server; sidecar tools call back agent_internal), settings, MinerU orchestration |
| **faq-service** | 8004 | FastAPI | FAQ KB CRUD, FAQ directory tree, Q&A pair CRUD, CSV/Excel batch import, FAQ search (exact + semantic fusion) |
| **kb-worker** | — | Celery (kb-api package) | Async ingestion pipeline: PENDING → PARSING (MinerU) → INDEXING (chunk+embed+ES) → COMPLETED |

Embedding & rerank models are **all external** (OpenAI-compatible `/embeddings` + `/rerank` APIs, configured via `EMBEDDING_*` / `RERANK_*`). kb-api does not load any local model; faq-service and kb-worker go through `/internal/embed` on kb-api, which forwards to the external API.

### Shared Package: kb-common

`services/kb-common/` (installed as editable `kb-common` via `uv`):

| Module | Contents |
|--------|----------|
| `kb_common/models.py` | All SQLAlchemy ORM models on shared PG: `User`, `ApiKey`, `KnowledgeBase`, `Directory`, `Document`, `Segment`, `FaqDirectory`, `FaqEntry`, `Setting` |
| `kb_common/database.py` | AsyncSession factory (`SessionLocal`) |
| `kb_common/config.py` | Pydantic Settings from `.env` |
| `kb_common/security.py` | JWT creation/verification, API key hashing, RBAC helpers |
| `kb_common/rag/` | `embedder.py` (external embeddings API), `chunker.py`, `indexer.py`, `searcher.py` (ES hybrid search), `reranker.py` (external rerank API, skipped when unconfigured), `tracer.py` (retrieval trace) |
| `kb_common/clients/` | `minio_client.py`, `es_client.py`, `mineru_client.py`, `llm_client.py` (OpenAI-compatible) |

### Processing Pipeline

```
Upload doc → kb-api (MinIO store + create DB record)
  → kb-api enqueues process_document.delay(doc_id) on Redis
    → kb-worker picks up:
      1. PARSING:  fetch raw from MinIO → MinerU parse → store markdown in MinIO
      2. INDEXING: chunker → embedder → ES index (kb_{kb_id})
         → write Segment rows → mark COMPLETED
```

### Data Model (all in kb_common/models.py, single PG database)

```
users ──► knowledge_bases (kb_type: DOCUMENT|FAQ)
              ├──► directories (tree)
              │       └──► documents ──► segments (chunks, linked to ES)
              └──► faq_directories (tree)
                      └──► faq_entries ──► segments (FAQ chunks, linked to ES)

api_keys ──► users
settings — runtime overrides for .env (LLM key, MinerU key, etc.)
```

ES index naming: `kb_{kb_id}` for document KBs, `faq_{kb_id}` for FAQ KBs.

## Frontend Design System

**Stack**: Vue 3.4+ (`<script setup lang="ts">`), TypeScript strict, Element Plus 2.x, Vite 5.x, Pinia, Vue Router 4, Axios, `@lucide/vue`(新代码图标), `@element-plus/icons-vue`(存量).

### Layout Pattern

Left sidebar (dark, 220px) + tab bar + main content (`#f5f7fa`). Navigation: 工作台 / 知识库 / 问答库 / 统一检索 / RAG 问答 / 模型配置 / 用户管理 / API Key 管理.

The tab bar (`stores/tabs.ts` + `components/layout/TabBar.vue`) maintains open pages — tabs auto-add on route navigation, with close/close-others/close-all support. 工作台 is permanent (unclosable).

### Color Palette

| Token | Value | Usage |
|-------|-------|-------|
| `--el-color-primary` | `#2B6BFF` | Primary buttons, links, active states (品牌蓝,见 frontend-dev-spec §3) |
| `--el-color-primary-light-9` | `#eef3ff` | Selected row bg, tag light bg |
| `--el-color-success` | `#67c23a` | Status: COMPLETED |
| `--el-color-warning` | `#e6a23c` | Status: PROCESSING/INDEXING/PARSING |
| `--el-color-danger` | `#f56c6c` | Delete, status: FAILED |
| `--el-color-info` | `#909399` | Secondary text |

### Route Structure

```
/login                    → Login page (public)
/                         → redirect /dashboard
/dashboard                → Workspace home
/knowledge-bases          → Card grid of document KBs
/knowledge-bases/:id      → Directory tree + document table
/knowledge-bases/:id/documents/:docId → Segment preview
/faq                      → FAQ KB list
/faq/:id                  → FAQ entries detail
/search                   → Unified search (with traceability drawer)
/chat                     → RAG Q&A
/settings                 → Model config (LLM/MinerU keys)
/admin/users              → User management (super_admin/admin)
/admin/api-keys           → API Key management (super_admin/admin)
```

### API Layer

All API calls go through `web/src/api/request.ts` — an Axios instance with JWT interceptor. Request interceptor attaches `Authorization: Bearer <token>`. Response interceptor redirects to `/login` on 401.

API modules in `web/src/api/`:
- `auth.ts` — login, /users/me
- `knowledge-base.ts` — KB CRUD
- `document.ts` — doc upload, list, status, segment preview
- `search.ts` — hybrid search
- `chat.ts` — RAG chat
- `faq.ts` — FAQ KB/entry CRUD
- `settings.ts` — runtime settings
- `users.ts` — admin user management

### Key Implementation Rules

- 所有数据库 schema 变更必须走 alembic 迁移：新增/修改 `kb_common/models.py` 字段时同步在 `alembic/versions/` 补迁移（先 `alembic heads` 确认无并行冲突），禁止依赖本地 create_all 或手工改表；部署时执行 `alembic upgrade head` 并验证受影响接口
- All components use Composition API with `<script setup lang="ts">`
- Pinia stores: `user.ts` (auth state), `app.ts` (sidebar collapse), `tabs.ts` (tab management)
- Chinese language interface throughout
- Match Element Plus conventions: `type="primary"` for create/submit, `type="danger"` for delete, `link` for inline actions
- `v-loading` directive on async areas, `el-empty` for empty states, `ElMessage.success/error()` for CUD feedback
- 前端统一开发规范（**强制**）：[docs/frontend-dev-spec.md](docs/frontend-dev-spec.md) —— 图标体系（新代码统一 `@lucide/vue` 具名导入、动态图标走 `utils/lucide-icons.ts` 注册表）、蓝色设计 Token（品牌主色 `#2B6BFF`）、TS 风格与工程门禁、AI Coding 约定；与 ui-guidelines.md 冲突时以本文件为准。

### 表格与弹窗布局要求

- UI/UED 设计规范（**强制**）：[docs/ui-guidelines.md](docs/ui-guidelines.md) —— 新增或修改任何界面前必须通读并按其「交付自检清单」核对；核心要求：布局稳定防跳动（切换模式用等宽占位而非移除控件）、说明文案默认 tooltip 不占行、反馈分层（Message/Alert/Tag）、主按钮唯一、状态色语义一致。
- 文档名、标题等主要内容列必须优先分配空间，使用 `min-width` 占据剩余宽度；状态、动作和开关列按实际控件宽度设置固定 `width`，不得让操作列挤占主要内容。
- 长名称使用 `show-overflow-tooltip` 保留完整内容的查看入口；不要通过缩小字号解决列宽问题。
- 单个开关仅展示当前状态文案，避免同时铺开开启和关闭标签；控件与文案应保持同一行，并提供可访问名称。
- 弹窗内表格需核对可用宽度与列宽总和。交付布局调整前检查长中文名称、开关两种状态、空数据及说明文字，避免控件换行、文字挤压和无意义留白。
- 列表「操作」列外显按钮最多 3 个（按使用频率选取），其余收进「更多」下拉，触发器以 `…` 图标表示；危险操作（删除等）归入「更多」并保留二次确认。按钮文案动词开头、明确动作（如「下载原文」而非「原文」）。
- 列表页统一范式（**强制**）：[docs/frontend-dev-spec.md §1.5](docs/frontend-dev-spec.md) —— 四段式结构（过滤栏/工具栏/表格/分页）、表格开启 `border` 支持表头拖拽调整列宽、过滤栏「查询/重置」按钮组 `margin-left: auto` 靠右、分页统一 KgPagination、操作按钮 `link + size="small"`；新增列表页必须照范式实现，参照 KnowledgeCenter.vue 与 DocumentLibraries.vue。

## .env Configuration

See README for the full `.env` template. Key optional vars:
- `LLM_API_KEY` — unset → chat returns friendly "configure LLM" message
- `MINERU_API_KEY` — unset → only txt/md/csv local parse; PDF/DOCX/PPTX/XLSX/HTML need this for MinerU cloud parse
- `LLM_BASE_URL` — OpenAI-compatible endpoint (default: GLM)
- `JWT_SECRET` — dev: `kb-mvp-dev-secret`

- **Design spec**: `docs/superpowers/specs/2026-07-28-knowledge-governance-expert-design.md` — authoritative
- **Style reference**: `风格参考/` — 5 screenshots of target UI appearance
- **Dify source** (for code reference): `dify/api/core/rag/` — splitter, extractor, embedding, retrieval, rerank patterns
- **Dify models**: `dify/api/models/dataset.py` — data model reference
- **Dify tasks**: `dify/api/tasks/` — Celery task patterns
