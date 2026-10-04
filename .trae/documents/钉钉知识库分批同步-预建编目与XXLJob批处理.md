# 钉钉知识库自动同步：预建编目 + XXL-Job 分批处理

## Context

「添加知识」选择钉钉知识库 + 自动同步后，点击确定：同步任务不进队列、文档不进列表。现状是 `create_source` 内 `trigger_first_sync` 线程池直跑全量同步（子进程模式）：必须先完整 walk_tree 遍历钉钉目录树才预建 SyncTask（大库分钟级，期间队列空）；LibraryDocument 在每篇实际处理（下载+上传 MinIO+阻塞等解析最长 600s/篇）后才逐篇创建；整个流程没有利用 XXL-Job 分批调度。

**目标**（用户已确认）：
1. 点击确定后，立即遍历目录树，全部文档一次性以「待同步」（PENDING）状态预建进文档列表，SyncTask（pending）预建进同步队列
2. 实际同步由 **XXL-Job 全局批处理 job** 驱动：默认每 30 秒一批、每批 5 篇（可配置），逐步把文档 PENDING→UPLOADED→PARSING→COMPLETED
3. Dify/RAGFlow 后端同步逻辑保持现状，仅 library 后端走新模式

## 方案总览

- **engine.py 完全不改**。library 模式不再进入 `SyncEngine._run_unlocked`，新增独立 `catalog.py`（编目）与 `batch.py`（批处理）。现有防御收尾（engine L279-284、finish_interrupted L60-64 标 failed）天然只作用于直跑模式。
- **仅一处 schema 变更**：`SyncTask.library_document_id`（String(36) nullable + 索引）+ alembic 迁移。`LibraryDocument.status` 是自由值 String 列，新增 'PENDING' 无需迁移；SyncRun 不加列（batch run 通过任务表派生判定）。
- **全局批处理 job**：executorHandler=`syncBatch`、空 param、`executorBlockStrategy=DISCARD_LATER`（长 tick 丢弃新触发，/idleBeat 回报 busy）。job id 不持久化，通过 admin pageList 按 handler 幂等注册。
- **锁**：编目与批处理均在 kb-api 进程内线程执行（不走路由子进程模式），沿用 `WORKER_LOCK(73102)` per-source 非阻塞互斥：编目全程持锁，批处理逐任务短暂持锁。

## 实施步骤

### 1. 模型与迁移

- [models.py](file:///Users/hjx/Documents/01_Project/knowledge-governance-expert/services/kb-common/kb_common/models.py) `SyncTask`（L589 附近）新增：

```python
library_document_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
```

- 新迁移 `alembic/versions/0050_sync_task_library_document.py`：先 `uv run alembic -c ../../alembic.ini heads` 确认唯一 head（当前应为 0049），`down_revision` 指向它；upgrade 加列 + `ix_sync_tasks_library_document_id` 索引
- [schemas.py](file:///Users/hjx/Documents/01_Project/knowledge-governance-expert/services/kb-api/app/schemas.py) `SyncTaskOut` 加 `library_document_id: str | None = None`

### 2. 配置项

[config.py](file:///Users/hjx/Documents/01_Project/knowledge-governance-expert/services/kb-common/kb_common/config.py) sync 配置区新增：

```python
sync_batch_size: int = Field(default=5, ge=1, le=50)            # 每批处理任务数
sync_batch_cron: str = "*/30 * * * *"                            # 批处理 cron（注册时转 Quartz）
sync_batch_stale_run_seconds: int = Field(default=21600, ge=600) # batch run 卡死收编阈值（6h）
sync_batch_running_reclaim_seconds: int = Field(default=1800, ge=60)  # running 任务自愈阈值（30min）
```

### 3. library_backend.py 改造

[library_backend.py](file:///Users/hjx/Documents/01_Project/knowledge-governance-expert/services/kb-api/app/services/sync/library_backend.py)：

1. `upload_file` 加可选 `doc_id` 参数：给定时更新预建行（FOR UPDATE 锁定 → MinIO 上传 `document-libraries/{library_id}/{doc.id}/{name}` → 回写 name/storage_path/size、status='UPLOADED'、message='' → 非阻塞提交解析 → 返回 `{"document": {"id": doc_id}}`）；为 None 保持现逻辑（新建 uuid + 阻塞等解析，兼容 legacy）
2. 新增 `_submit_parse(doc_id)`：`asyncio.run` 只做 `_do_parse`（上传 MinerU + create_job + status→PARSING），不进轮询循环；异常上抛由批处理标 failed。txt/md/csv 会同步完成直接 COMPLETED，无需刷新
3. 删除文档时级联取消关联 pending 任务：`SyncTask.library_document_id == str(doc_id) AND status=='pending'` → failed「目标文档已删除，任务取消」

### 4. 新模块 catalog.py（编目）

新文件 `services/kb-api/app/services/sync/catalog.py`，`run_catalog(source_id, trigger="manual", operator="") -> dict`：

1. `source_lock(source_id, WORKER_LOCK)` 非阻塞；拿不到 → `{"status": "skipped"}`
2. **该 source 已有 status=='running' 的 SyncRun → 跳过本轮编目**（记 SyncLog），这是 cron 防重复 run 的机制
3. 建 `SyncRun(running, message='编目中：遍历钉钉目录树')`
4. `resolve_source_operator_sync` → `make_dingtalk_client`；`library_id = int(source.dify_dataset_id)`（照抄 `make_library_backend` 的转换）
5. `walk_tree(use_cache=False)` → `_parse_whitelist` 白名单过滤 → `skip_reason(node, s, "library")` 过滤
6. **删除同步照旧立即执行**（平移 engine L182-215 语义）：钉钉侧消失且 policy=sync → 建 delete task（直接 success）+ `backend.delete_document` + 删 mapping + `deleted_count+1`
7. **逐 node 预建**（一次 commit 落库）：
   - `mapping.status=='synced'` 且 `_meta_hash(node)` 非空且等于 `mapping.meta_hash` → 跳过（增量编目，复用 engine `_meta_hash`；无时间戳节点靠批处理下载后 content_hash 跳过）
   - mapping 不存在 → 新建（status='pending'）；`mapping.dify_document_id` 对应行存在 → 复用 doc（update 场景），不存在 → 新建 `LibraryDocument(id=uuid4, name=节点名, storage_path="", size=0, status="PENDING", progress=0, source="dingtalk")`，`mapping.dify_document_id = str(doc_id)`
   - `SyncTask(run_id, source_id, kind="sync", action=('update' if 上轮 synced else 'create'), node_id, name[:500], file_ext=node_extension(node), dataset_id=str(library_id), status="pending", library_document_id=str(doc_id), trigger, operator)`——**注意 action 判定不能沿用"dify_document_id 有值即 update"（编目已提前写入该列），要看 mapping 上轮 status**
8. `run.total = 任务数`；任务数为 0 → 立即收尾 run success
9. 收尾前 best effort：`ensure_batch_job_running()` + `trigger_batch_once()`（失败仅 WARNING，30s 内 cron 自然接管）

### 5. 新模块 batch.py（批处理）

新文件 `services/kb-api/app/services/sync/batch.py`，`run_sync_batch_tick(job_log=None) -> tuple[int, str]`，每轮四段：

**(a) 自愈**：`status=='running' AND library_document_id IS NOT NULL AND started_at < now()-sync_batch_running_reclaim_seconds` → 回退 pending

**(b) refresh 兜底**：查 library 源 mapping 指向且 status=='PARSING' 的 LibraryDocument（两步查询：先取 distinct diffy_document_id 再转 UUID in 查询），逐个 `asyncio.run(refresh_state(adapter, session, lib, doc))`——必须加载 KnowledgeLibrary（`_finalize_parse` 要调 `_processing_for(lib, doc)`）；失败仅记日志

**(c) 原子认领**：

```python
select(SyncTask).where(
    status=='pending', kind=='sync', action.in_(('create','update')),
    or_(library_document_id.isnot(None), source_id.in_(select(SyncSource.id).where(backend_type=='library'))),
).order_by(SyncTask.id).limit(s.sync_batch_size).with_for_update(skip_locked=True)
# 标 running + started_at，commit 释放行锁
```

**(d) 逐任务处理**：
- 前置守卫：source 不存在/disabled → task failed「同步源已停用或已删除」；`library_document_id` 行不存在（用户手删）→ task failed「目标文档已被删除」+ mapping status='error'（下轮编目会预建新行重同步）
- `source_lock(source_id, WORKER_LOCK)` 非阻塞，抢不到 → task 回退 pending 下轮再试
- `dt.get_node(task.node_id)` 取最新元数据 → 复用 `SyncEngine._fetch`（私有先例：task_retry 已有同款调用）下载 + sha256
- **增量跳过**：`mapping.status=='synced'` 且 content_hash 相同 → task success + 刷新 meta_hash，return
- `backend.upload_file(str(library_id), local_file, file_name=..., doc_id=task.library_document_id)`（更新预建行 + 非阻塞提交解析）
- 回写 mapping：name/category/meta_hash/content_hash/status='synced'/error=''/last_synced_at；task success：file_size/file_ext/finished_at
- run 计数**原子自增**（SQL `update(SyncRun).values(created_count=SyncRun.created_count+1)`），避免批次并发丢失更新

**(e) run 收尾 `_finalize_run_if_done(run_id)`**（每任务处理后调用，幂等）：该 run 无 pending/running 任务 → 按 engine L268-276 规则置 success/partial/failed（skipped 数用 `total-(created+updated+deleted+failed)` 近似）+ finished_at + 汇总 message

### 6. scheduler.py 调度改造

[scheduler.py](file:///Users/hjx/Documents/01_Project/knowledge-governance-expert/services/kb-api/app/services/sync/scheduler.py)：

1. `_handle_sync_source`（L81-106）分流：source.backend_type=='library' → 调 `run_catalog(source_id, trigger, operator)`；否则原 `run_sync`
2. 新增 `_handle_sync_batch(param, job_log)` → `run_sync_batch_tick(job_log)`
3. 新增 `_batch_job_spec(group_id)`：jobDesc「知识同步批处理（全局）」、executorHandler='syncBatch'、executorParam=''、**executorBlockStrategy='DISCARD_LATER'**、cron=crontab_to_quartz(s.sync_batch_cron)
4. 新增 `ensure_batch_job_running()`：`find_job_by_handler("syncBatch")` 幂等注册（找到→update+start；没有→add_job），模块级缓存 `_batch_job_id`；追加到 `sync_all_jobs()`（L147-157）末尾
5. 新增 `trigger_batch_once()`：xxl 启用→`admin.trigger_job(_batch_job_id)`；未启用→`_direct_executor.submit(run_sync_batch_tick)`
6. `trigger_first_sync`（L191-199）分流：library → `_direct_executor.submit(run_catalog, source_id, "manual", operator)`
7. `trigger_source_sync`（L160-188）direct 分支对 library 源同样分流到 run_catalog
8. **降级模式**（XXL_JOB_ENABLED=false）：`start_sync_scheduler` 起 daemon 线程每 30s 调一次 `run_sync_batch_tick()`
9. L265 注册双 handler：`{"syncSource": _handle_sync_source, "syncBatch": _handle_sync_batch}`

[xxljob_admin.py](file:///Users/hjx/Documents/01_Project/knowledge-governance-expert/services/kb-api/app/services/sync/xxljob_admin.py) 新增 `find_job_by_handler(executor_handler, job_group)`：pageList 按 executorHandler 过滤遍历匹配。

### 7. runtime.py 中断收编改造（关键坑）

[runtime.py](file:///Users/hjx/Documents/01_Project/knowledge-governance-expert/services/kb-api/app/services/sync/runtime.py) `recover_interrupted_runs`（L75-84，30s 全表扫 running run）：**不改会把跨批次长驻 running 的健康 batch run 误杀**。改造：

- 派生判定 batch run：`SyncTask.library_document_id IS NOT NULL` 的 run_id 集合
- batch run：仅当 `started_at < now()-sync_batch_stale_run_seconds`（批处理 job 死亡）才 `finish_interrupted(run_id, '批处理长时间未推进...')`（finish_interrupted 标 failed 语义此处正是所需）；否则 continue 跳过
- 非 batch run（dify/ragflow）：保持原 per-source 双锁收编逻辑不变

### 8. task_retry + 路由守卫

- [task_retry.py](file:///Users/hjx/Documents/01_Project/knowledge-governance-expert/services/kb-api/app/services/sync/task_retry.py)：library 任务（`library_document_id` 非空）重试改为**重新入队**（status='pending'、retry_count+1），不走 `_process_node`；该文件中 WORKER_LOCK 运行中判断对 library 源放行
- [sync_route.py](file:///Users/hjx/Documents/01_Project/knowledge-governance-expert/services/kb-api/app/routes/sync_route.py) `sync_source_now`（L417-437）：running run 409 守卫对 library 源放行（catalog 内部决定 skip/执行）
- [managed_library.py](file:///Users/hjx/Documents/01_Project/knowledge-governance-expert/services/kb-api/app/services/managed_library.py)：PENDING 守卫——`parse`/`original`/`preview_original` 409「文档待同步」；`export_library` 存在 PENDING 文档 → 409；`delete_document` 允许删 PENDING 并级联取消其 pending task（级联逻辑与步骤 3 共用，注意异步/同步 session 差异）

### 9. 前端

- [DocumentLibraries.vue](file:///Users/hjx/Documents/01_Project/knowledge-governance-expert/web/src/views/knowledge-libraries/DocumentLibraries.vue)：
  - `statusLabels`（L101）加 `PENDING: '待同步'`、`UPLOADED: '已上传'`；状态 tag PENDING 用 warning 色
  - `poll()`（L256-267）扩展：列表存在 PENDING 文档时定期 reload 整表（批处理推进状态后自动感知）
  - PENDING 文档：文档名 link 与「下载原文/重新解析」禁用或隐藏
- [AddKnowledgeDialog.vue](file:///Users/hjx/Documents/01_Project/knowledge-governance-expert/web/src/components/kb/AddKnowledgeDialog.vue) L242/L260：成功提示改「已创建自动同步任务（文档将分批进入文档列表，进度可在同步队列查看）」，逻辑零改动
- [SyncQueue.vue](file:///Users/hjx/Documents/01_Project/knowledge-governance-expert/web/src/views/governance/collection/SyncQueue.vue)：可选小改——首次加载 `tabs.pending > 0` 时默认选中「待处理」页签；其余（三页签、5s 轮询）无需改

## 数据流时序

```
T0    确定点击 → POST /sync/sources → create_source：
        reload_sync_jobs()（注册 source cron 作业 + 幂等注册 syncBatch 全局作业）
        → trigger_first_sync → 线程池 submit(run_catalog) → 接口立即返回
T0+ε  run_catalog：WORKER_LOCK → 无 running run → 建 SyncRun#1 → walk_tree（0.4s节流）
        → 删除同步立即执行 → 预建 N×[LibraryDocument(PENDING) + SyncTask(pending)
          + mapping(pending)] → 前端文档列表刷出 N 行「待同步」+ 队列 N 个待处理
        → trigger_batch_once()
T0+30s起 syncBatch 每 30s：自愈 → refresh PARSING → SKIP LOCKED 认领最早 5 个 pending
        → 逐个：锁源 → get_node → 下载 → hash 未变跳过 / 变则 upload(doc_id=预建行)
          → UPLOADED → 非阻塞提交解析 → PARSING → mapping synced → task success
        → run 计数原子 +1 → 该 run 无 pending/running → finalize
每 5 分钟  cron syncSource → 分流 run_catalog：上轮 run 仍 running → 跳过；
        已终态 → 增量编目（meta_hash 命中不建任务；新文档入队；消失文档按策略删）
```

## 边缘情况

- 多 tick 并发：单 jobId 串行 + DISCARD_LATER + SKIP LOCKED 三重防护
- 编目 vs 批处理：编目全程持 WORKER_LOCK；批处理逐任务抢锁，抢不到回退 pending；不嵌套持锁，无死锁
- 失败重试：task failed → 队列页重试 → library 任务重入队 pending，下个 tick 处理
- kb-api 重启：认领中 running 任务 >30min 自愈回退 pending；PARSING 文档靠 MinerU 侧 job + tick refresh 续收
- 批处理 job 死亡：recovery 按 6h stale 收编（pending 标 failed 可重试）
- 删 COMPLETED 文档：mapping 残留 → 下轮编目发现 doc 缺失 → 预建新行重新同步（镜像语义，不同步某文档应走白名单/停用）

## 验证

1. `alembic upgrade head` 应用迁移；启动 kb-api、kb-worker、前端
2. 「添加知识」选钉钉知识库 + 自动同步 + 确定：admin 出现「知识同步批处理（全局）」作业（RUNNING、cron `0/30 * * * * ?`）；文档列表 30s 内出现 N 行「待同步」；同步队列「待处理」N 个
3. 观察 30s 粒度推进：队列待处理递减（每轮 ≤5）、文档逐批 UPLOADED→PARSING→COMPLETED、admin 调度日志每 30s 一条 syncBatch；SQL 抽查 sync_tasks/library_documents/sync_runs 分组计数
4. 增量轮：改一篇钉钉文档 → 下个 cron 编目只入队该篇（meta_hash 变化）；未变文档不出现
5. 边缘：手删 PENDING 文档（任务变 failed）；断 MinerU 构造失败 → 队列重试 → 恢复后成功；XXL_JOB_ENABLED=false 降级直跑 tick
6. 回归：Dify/RAGFlow 源同步行为不变（engine 未动）；`pnpm type-check`；`./scripts/smoke_test.sh`
