"""library 同步批处理：XXL-Job 全局作业每轮消费一批待处理任务（默认 30s × 5 篇）。

每轮四段：
(a) 自愈：回收卡死 running 的批处理任务（kb-api 崩溃/被 kill 后悬挂回退 pending）；
(b) refresh：跟进上一批 PARSING 中的同步文档（查 MinerU job、完成时落地分段），
    不依赖前端轮询；失败仅记日志；
(c) 认领：FOR UPDATE SKIP LOCKED 取最早 N 个 pending 任务（跨源全局 FIFO）；
(d) 逐任务：锁源 → 现取节点元数据 → 下载 → content_hash 未变跳过 /
    更新预建 LibraryDocument 行并提交解析 → 回写映射 → 任务终态；
(e) 收尾：任务所属 run 若全部任务终态 → 按既有规则置 success/partial/failed。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import or_, select, update

from kb_common.config import get_settings
from kb_common.models import (
    KnowledgeLibrary, LibraryDocument, SyncDocumentMapping, SyncFailure, SyncLog,
    SyncRun, SyncSource, SyncTask,
)

from .engine import SyncEngine, _local_storage_dir, _meta_hash
from .export_service import safe_name
from .runtime import WORKER_LOCK, source_lock
from .sync_database import SyncSessionLocal
from .sync_settings import make_dingtalk_client, make_library_backend

logger = logging.getLogger(__name__)


def run_sync_batch_tick(job_log=None) -> tuple[int, str]:
    """批处理一轮入口（XXL-Job syncBatch handler / 降级线程共用）。"""
    s = get_settings()
    tasks: list[int] = []
    try:
        with SyncSessionLocal() as db:
            # (a) 自愈：认领后进程中断的 running 任务回退 pending（下轮重新处理）
            reclaim_before = datetime.utcnow() - timedelta(seconds=s.sync_batch_running_reclaim_seconds)
            db.query(SyncTask).filter(
                SyncTask.status == "running",
                SyncTask.library_document_id.isnot(None),
                SyncTask.started_at.isnot(None),
                SyncTask.started_at < reclaim_before,
            ).update({"status": "pending", "error": ""},
                     synchronize_session=False)
            db.commit()

            # (b) refresh：兜底收割上一批 PARSING 文档的解析状态
            _refresh_parsing_documents(db)

            # (c) 原子认领：跨源全局 FIFO，SKIP LOCKED 防多 tick 并发重复领取
            library_source_ids = select(SyncSource.id).where(SyncSource.backend_type == "library")
            claimed = db.execute(
                select(SyncTask)
                .where(SyncTask.status == "pending", SyncTask.kind == "sync",
                       SyncTask.action.in_(("create", "update")),
                       or_(SyncTask.library_document_id.isnot(None),
                           SyncTask.source_id.in_(library_source_ids)))
                .order_by(SyncTask.id).limit(s.sync_batch_size)
                .with_for_update(skip_locked=True)
            ).scalars().all()
            for t in claimed:
                t.status = "running"
                t.started_at = datetime.utcnow()
                t.finished_at = None
                t.error = ""
                tasks.append(t.id)
            db.commit()  # 释放行锁；running 即「已认领」标记
    except Exception as exc:  # noqa: BLE001
        logger.exception("批处理认领失败")
        if job_log is not None:
            job_log.log(f"批处理认领失败: {exc}")
        return 500, f"批处理认领失败: {exc}"

    ok = failed = 0
    for task_id in tasks:
        outcome, _error = _process_batch_task(task_id)
        if outcome == "failed":
            failed += 1
        elif outcome != "skipped":
            ok += 1
    message = f"批处理完成：领取 {len(tasks)}，成功 {ok}，失败 {failed}"
    logger.info(message)
    if job_log is not None:
        job_log.log(message)
    return 200, message


# ---------- (b) 解析状态兜底 ----------

def _refresh_parsing_documents(db) -> None:
    """刷新 library 同步源关联的 PARSING 文档（MinerU job 查询 + 完成时落地分段）。"""
    doc_id_strs = [row[0] for row in db.execute(
        select(SyncDocumentMapping.dify_document_id).distinct()
        .join(SyncSource, SyncSource.id == SyncDocumentMapping.source_id)
        .where(SyncSource.backend_type == "library",
               SyncDocumentMapping.dify_document_id.isnot(None))
    ).all()]
    if not doc_id_strs:
        return
    uuids = []
    for v in doc_id_strs:
        try:
            uuids.append(uuid.UUID(v))
        except ValueError:
            continue
    rows = db.query(LibraryDocument.id, LibraryDocument.library_id).filter(
        LibraryDocument.id.in_(uuids), LibraryDocument.status == "PARSING").all()
    if not rows:
        return
    try:
        asyncio.run(_refresh_parsings(set(rows)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("批处理刷新解析状态失败: %s", exc)


async def _refresh_parsings(pairs: set) -> None:
    from kb_common.database import fresh_session

    from app.routes.managed_library import engine as get_engine
    from app.routes.managed_library import refresh_state
    adapter = get_engine()
    # 独立短命引擎：执行器线程的临时事件循环不能复用绑定主 loop 的全局连接池
    async with fresh_session() as s:
        for doc_id, library_id in pairs:
            try:
                lib = await s.get(KnowledgeLibrary, library_id)
                doc = await s.get(LibraryDocument, doc_id)
                if lib is None or doc is None:
                    continue
                await refresh_state(adapter, s, lib, doc)
                await s.commit()
            except Exception as exc:  # noqa: BLE001
                logger.warning("刷新文档 %s 解析状态失败: %s", doc_id, exc)


# ---------- (d) 逐任务处理 ----------

def _process_batch_task(task_id: int) -> tuple[str, str]:
    """处理单个已认领任务；返回 (outcome, error)。"""
    with SyncSessionLocal() as db:
        task = db.get(SyncTask, task_id)
        if task is None or task.status != "running":
            return "skipped", "任务已不在处理中"
        source = db.get(SyncSource, task.source_id) if task.source_id else None
        if source is None or not source.enabled:
            _finish_task(db, task, "failed", "同步源已停用或已删除，任务取消")
            _bump_run(db, task.run_id, "failed")
            _finalize_run_if_done(db, task.run_id)
            return "failed", "同步源已停用或已删除"
        # 预建文档行已不存在（用户手动删除）：任务取消；下轮编目会预建新行重新同步
        if task.library_document_id:
            try:
                doc_uuid = uuid.UUID(task.library_document_id)
            except ValueError:
                doc_uuid = None
            doc_exists = doc_uuid is not None and db.query(LibraryDocument.id).filter_by(
                id=doc_uuid).first() is not None
            if not doc_exists:
                mapping = _mapping_for(db, source.id, task.node_id)
                if mapping is not None:
                    mapping.status = "error"
                    mapping.error = "目标文档已被删除"
                _finish_task(db, task, "failed", "目标文档已被删除，任务取消")
                _bump_run(db, task.run_id, "failed")
                _finalize_run_if_done(db, task.run_id)
                return "failed", "目标文档已被删除"
        # 源被编目/同批其他任务占用：回退 pending，下轮再试
        with source_lock(source.id, WORKER_LOCK) as free:
            if not free:
                task.status = "pending"
                task.started_at = None
                task.error = ""
                db.commit()
                return "skipped", "同步源正忙，任务回退待处理"
            return _process_locked(db, task, source)


def _process_locked(db, task: SyncTask, source: SyncSource) -> tuple[str, str]:
    """已持有源锁的单任务处理：下载 → 增量跳过/上传 → 回写映射与任务状态。"""
    dt = None
    backend = None
    node_id = task.node_id or ""
    name = task.name
    try:
        from app.services.dingtalk_operator import resolve_source_operator_sync
        op_union, _tag = resolve_source_operator_sync(db, source)
        dt = make_dingtalk_client(db, operator_union_id=op_union)
        backend = make_library_backend(source)
        library_id = backend.library_id

        # 节点元数据现取（永远新鲜；钉钉侧已删除时由此报错）
        response = dt.get_node(node_id)
        node = response.get("node") or response
        if not node.get("name"):
            raise RuntimeError("钉钉未返回节点元数据，无法确定源文件类型")
        node = {**node, "nodeId": node_id}

        # 下载复用引擎 _fetch（含源文档 MinIO 备份）
        engine = SyncEngine(source)
        rel_dir = Path(node.get("relative_dir", "").strip("/"))
        base_dir = _local_storage_dir() / str(source.id)
        local_file, content_hash = engine._fetch(dt, node, name, base_dir, rel_dir)

        mapping = _mapping_for(db, source.id, node_id)
        # 增量跳过：上一轮已同步且内容未变化（与 engine _process_node 同口径）
        if (mapping is not None and mapping.status == "synced"
                and mapping.content_hash == content_hash):
            if task is not None:
                task.status = "success"
                task.finished_at = datetime.utcnow()
                task.error = ""
            mapping.name = name
            mapping.meta_hash = _meta_hash(node)
            db.commit()
            _finalize_run_if_done(db, task.run_id)
            return "skipped", ""

        was_update = task.action == "update"
        # 更新预建行 + 非阻塞提交解析（UPLOADED → PARSING；txt/md/csv 直接 COMPLETED）
        result = backend.upload_file(str(library_id), local_file,
                                      file_name=local_file.name,
                                      doc_id=task.library_document_id)
        doc_id = (result.get("document") or {}).get("id")
        if not doc_id:
            raise RuntimeError("目标文档库上传未返回文档 ID")

        # 回写映射（上传成功即视为同步完成；解析由 tick refresh 兜底收割）
        if mapping is None:
            mapping = SyncDocumentMapping(source_id=source.id, node_id=node_id,
                                          name=name, category=node.get("category", ""))
            db.add(mapping)
        mapping.dify_document_id = doc_id
        mapping.parent_node_id = node.get("parent_node_id", source.root_node_id)
        mapping.name = name
        mapping.relative_path = str(rel_dir / safe_name(name))
        mapping.category = node.get("category", "")
        mapping.meta_hash = _meta_hash(node)
        mapping.content_hash = content_hash
        mapping.local_path = str(local_file)
        mapping.status = "synced"
        mapping.error = ""
        mapping.last_synced_at = datetime.utcnow()

        task.status = "success"
        task.finished_at = datetime.utcnow()
        task.error = ""
        try:
            task.file_size = local_file.stat().st_size
        except OSError:
            task.file_size = None
        task.file_ext = local_file.suffix.lower().lstrip(".") or task.file_ext
        db.query(SyncFailure).filter_by(source_id=source.id, node_id=node_id).delete()
        db.commit()
        _bump_run(db, task.run_id, "updated" if was_update else "created")
        _finalize_run_if_done(db, task.run_id)
        return ("updated" if was_update else "created"), ""
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        logger.error("批处理任务失败 [%s]: %s", name, error)
        db.rollback()
        task = db.get(SyncTask, task.id)
        if task is not None:
            _record_task_failure(db, task, source, node_id, name, error)
        return "failed", error
    finally:
        if dt is not None:
            dt.close()
        if backend is not None:
            backend.close()


def _record_task_failure(db, task: SyncTask, source: SyncSource,
                         node_id: str, name: str, error: str) -> None:
    """任务失败落库：task failed + mapping error + SyncFailure + 预建文档标 FAILED。"""
    task.status = "failed"
    task.finished_at = datetime.utcnow()
    task.error = error[:2000]
    mapping = _mapping_for(db, source.id, node_id)
    if mapping is not None:
        mapping.status = "error"
        mapping.error = error[:2000]
    else:
        db.add(SyncDocumentMapping(source_id=source.id, node_id=node_id, name=name,
                                   status="error", error=error[:2000]))
    db.add(SyncFailure(run_id=task.run_id, source_id=source.id, node_id=node_id,
                       name=name, error=error[:2000]))
    # 下载/上传/解析提交失败：把预建文档行标 FAILED（仍在 PENDING/UPLOADED 时）
    if task.library_document_id:
        try:
            doc = db.query(LibraryDocument).filter_by(
                id=uuid.UUID(task.library_document_id)).first()
            if doc is not None and doc.status in ("PENDING", "UPLOADED", "PARSING"):
                doc.status = "FAILED"
                doc.message = f"同步失败: {error}"[:2000]
        except ValueError:
            pass
    db.commit()
    _bump_run(db, task.run_id, "failed")
    _finalize_run_if_done(db, task.run_id)


# ---------- 辅助 ----------

def _mapping_for(db, source_id: int, node_id: str | None):
    if not node_id:
        return None
    return db.query(SyncDocumentMapping).filter_by(
        source_id=source_id, node_id=node_id).first()


def _finish_task(db, task: SyncTask, status: str, message: str) -> None:
    task.status = status
    task.finished_at = datetime.utcnow()
    task.error = "" if status == "success" else message[:2000]
    db.commit()


def _bump_run(db, run_id: int | None, field: str) -> None:
    """run 计数原子自增（仅 running 中的 run；终态 run 不再回写，审计以任务行为准）。"""
    if run_id is None:
        return
    column = {"created": SyncRun.created_count, "updated": SyncRun.updated_count,
              "failed": SyncRun.failed_count}[field]
    db.execute(update(SyncRun).where(SyncRun.id == run_id, SyncRun.status == "running")
               .values({column: column + 1}))
    db.commit()


def _finalize_run_if_done(db, run_id: int | None) -> None:
    """run 的全部任务终态时收尾（幂等；判定规则与 engine 直跑模式一致）。"""
    if run_id is None:
        return
    run = db.query(SyncRun).filter_by(id=run_id, status="running").with_for_update().first()
    if run is None:
        return
    remaining = db.query(SyncTask).filter(
        SyncTask.run_id == run_id,
        SyncTask.status.in_(("pending", "running")),
    ).count()
    if remaining:
        return
    created = run.created_count or 0
    updated = run.updated_count or 0
    deleted = run.deleted_count or 0
    failed = run.failed_count or 0
    total = run.total or 0
    skipped = max(total - created - updated - deleted - failed, 0)
    run.finished_at = datetime.utcnow()
    if failed == 0:
        run.status = "success"
    elif created + updated + deleted + skipped == 0:
        run.status = "failed"
    else:
        run.status = "partial"
    run.message = (f"批处理完成 total={total} skipped={skipped} created={created} "
                   f"updated={updated} deleted={deleted} failed={failed}")
    db.add(SyncLog(run_id=run_id,
                   level="INFO" if run.status == "success" else "WARNING",
                   message=run.message))
    db.commit()
