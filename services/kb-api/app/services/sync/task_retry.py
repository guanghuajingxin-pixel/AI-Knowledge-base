"""同步队列：失败任务的任务级重试（单文档重处理，不整源重跑）。

与整源重试（/sync/failures/{id}/retry → run_sync）的区别：
- 只重新下载/上传这一个钉钉节点，更新同一映射与同一任务行（retry_count 累加）；
- 不产生新的 SyncRun，失败记录 run_id 为空；
- 通过 WORKER_LOCK 与整源同步互斥，避免同源并发写同一 Dify 文档。
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from kb_common.clients.dify_document import dataset_runtime
from kb_common.models import SyncDocumentMapping, SyncSource, SyncTask

from .engine import SyncEngine, _local_storage_dir
from .runtime import WORKER_LOCK, source_lock
from .sync_database import SyncSessionLocal
from .sync_settings import make_backend, make_dingtalk_client

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=2)


def submit_task_retry(task_id: int) -> None:
    """提交到后台线程执行，接口立即返回。"""
    _executor.submit(retry_task, task_id)


def retry_task(task_id: int) -> dict:
    """重处理单个失败任务；返回 {ok, message}。

    library 批处理模式任务（library_document_id 非空 / 源 backend_type=library）
    改为重新入队 pending，由批处理作业分批消费（与批处理并发安全）；
    dify/ragflow 任务保持直跑重处理。
    """
    with SyncSessionLocal() as db:
        task = db.get(SyncTask, task_id)
        if task is None:
            return {"ok": False, "message": "任务不存在"}
        if task.status != "failed":
            return {"ok": False, "message": "仅失败任务可重试"}
        if not task.node_id or task.source_id is None:
            return {"ok": False, "message": "任务缺少钉钉节点或同步源信息，无法重试"}
        source_id = task.source_id
        is_library = bool(task.library_document_id)
        if not is_library:
            source = db.get(SyncSource, source_id)
            is_library = source is not None and (source.backend_type or "").strip().lower() == "library"

    if is_library:
        with SyncSessionLocal() as db:
            task = db.get(SyncTask, task_id)
            if task is None or task.status != "failed":
                return {"ok": False, "message": "任务已不在失败状态"}
            task.status = "pending"
            task.started_at = None
            task.finished_at = None
            task.error = ""
            task.retry_count = (task.retry_count or 0) + 1
            db.commit()
        return {"ok": True, "message": "任务已重新入队，等待批处理分批同步"}

    with source_lock(source_id, WORKER_LOCK) as free:
        if not free:
            return {"ok": False, "message": "该同步源正在同步中，请等待结束后再重试"}
        return _do_retry(task_id, source_id)


def _do_retry(task_id: int, source_id: int) -> dict:
    db = SyncSessionLocal()
    try:
        task = db.get(SyncTask, task_id)
        source = db.get(SyncSource, source_id)
        if task is None or source is None:
            return {"ok": False, "message": "任务或同步源不存在"}
        if task.status != "failed":
            return {"ok": False, "message": "仅失败任务可重试"}
        task.status = "running"
        task.started_at = datetime.utcnow()
        task.finished_at = None
        task.error = ""
        task.retry_count = (task.retry_count or 0) + 1
        db.commit()

        from app.services.dingtalk_operator import resolve_source_operator_sync
        op_union, _op_tag = resolve_source_operator_sync(db, source)
        dt = make_dingtalk_client(db, operator_union_id=op_union)
        backend = make_backend(source, db)
        try:
            response = dt.get_node(task.node_id)
            node = response.get("node") or response
            if not node.get("name"):
                raise ValueError("钉钉未返回节点元数据，无法确定源文件类型")
            node = {**node, "nodeId": task.node_id}
            dataset = backend.resolve_dataset(source.dify_dataset_id, source.dify_dataset_name)
            dataset_id = dataset["id"]
            dataset_name = dataset.get("name") or source.dify_dataset_name
            runtime_mode = (dataset_runtime(dataset)
                            if (source.backend_type or "dify") == "dify" else "general")
            pipeline_inputs: dict = {}
            if runtime_mode == "rag_pipeline":
                from .dify_pipeline_vars import resolve_pipeline_inputs
                raw = (getattr(source, "pipeline_inputs", "") or "").strip()
                try:
                    parsed = json.loads(raw) if raw else {}
                    pipeline_inputs = parsed if isinstance(parsed, dict) else {}
                except (ValueError, TypeError):
                    pipeline_inputs = {}
                pipeline_inputs = resolve_pipeline_inputs(
                    dataset_id, backend.local_file_node_id(dataset_id), pipeline_inputs)
            mapping = db.query(SyncDocumentMapping).filter_by(
                source_id=source.id, node_id=task.node_id).first()
            base_dir = _local_storage_dir() / str(source.id)
            engine = SyncEngine(source)
            outcome, error = engine._process_node(
                db, None, source, node, mapping, base_dir, dataset_id, dataset_name,
                runtime_mode, pipeline_inputs, dt, backend, task=task)
            db.commit()
            if outcome == "failed":
                return {"ok": False, "message": error or "重试失败"}
            return {"ok": True, "message": "重试完成，文档已重新同步"}
        finally:
            dt.close()
            backend.close()
    except Exception as exc:  # noqa: BLE001
        logger.exception("任务级重试异常 task_id=%s", task_id)
        db.rollback()
        task = db.get(SyncTask, task_id)
        if task is not None:
            task.status = "failed"
            task.finished_at = datetime.utcnow()
            task.error = f"重试异常: {exc}"[:2000]
            db.commit()
        return {"ok": False, "message": str(exc)}
    finally:
        db.close()
