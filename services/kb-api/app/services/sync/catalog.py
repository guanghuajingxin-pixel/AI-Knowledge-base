"""library 后端编目：遍历钉钉目录树 → 预建同步任务与待同步文档（PENDING 占位）。

与 batch.py（XXL-Job 批处理消费）配对，替代 library 模式下的全量直跑：
- 创建同步源 / 手动同步 / cron 周期 → run_catalog：任务立即进同步队列、
  文档立即以「待同步」（PENDING）状态进文档列表（一次编目、分批消费）；
- 增量编目：mapping 已 synced 且节点元数据指纹（meta_hash）未变 → 不重建任务；
  新增/变更节点 → 预建 LibraryDocument(PENDING) + SyncTask(pending) + mapping(pending)；
- 钉钉侧消失的文档按 delete_policy 照旧立即删除（不进批处理）。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path

from kb_common.config import get_settings
from kb_common.models import (
    LibraryDocument, SyncDocumentMapping, SyncLog, SyncRun, SyncSource, SyncTask,
)

from .engine import _meta_hash, _parse_whitelist
from .runtime import WORKER_LOCK, source_lock
from .source_files import node_extension, skip_reason
from .sync_database import SyncSessionLocal
from .sync_settings import make_dingtalk_client, make_library_backend

logger = logging.getLogger(__name__)


def _log(db, run_id: int | None, level: str, message: str) -> None:
    db.add(SyncLog(run_id=run_id, level=level, message=message))
    logger.info("[%s] %s", run_id, message)


def run_catalog(source_id: int, trigger: str = "manual", operator: str = "") -> dict:
    """编目一个 library 同步源：预建任务/文档/映射，实际同步由批处理分批消费。

    返回 {run_id, status, total, queued, deleted, skipped, message}。
    """
    with source_lock(source_id, WORKER_LOCK) as free:
        if not free:
            return {"status": "skipped", "message": "该同步源正在同步"}
        db = SyncSessionLocal()
        run_id: int | None = None
        try:
            source = db.get(SyncSource, source_id)
            if source is None or not source.enabled:
                return {"status": "skipped", "message": "同步源不存在或已停用"}
            # 上轮批处理尚未消费完：跳过本轮编目，避免重复 run / 重复任务
            active = db.query(SyncRun).filter_by(source_id=source_id, status="running").first()
            if active is not None:
                _log(db, active.id, "INFO", "上一轮批处理尚未完成，跳过本轮编目")
                db.commit()
                return {"run_id": active.id, "status": "skipped",
                        "message": "上一轮批处理尚未完成，等待同步队列消费"}

            run = SyncRun(source_id=source_id, trigger=trigger, status="running",
                          message="编目中：遍历钉钉知识库目录树",
                          operator=operator or ("系统" if trigger == "schedule" else ""))
            db.add(run)
            db.commit()
            run_id = run.id

            from app.services.dingtalk_operator import resolve_source_operator_sync
            op_union, op_tag = resolve_source_operator_sync(db, source)
            _log(db, run_id, "INFO", f"钉钉操作人身份：{op_tag}")
            dt = make_dingtalk_client(db, operator_union_id=op_union)
            backend = make_library_backend(source)
            library_id = backend.library_id
            try:
                s = get_settings()
                nodes = dt.walk_tree(source.root_node_id, s.sync_max_depth,
                                     s.sync_max_results_per_page, use_cache=False)
                whitelist = _parse_whitelist(getattr(source, "node_whitelist", "") or "")
                if whitelist:
                    nodes = [n for n in nodes if n.get("nodeId") in whitelist]
                    _log(db, run_id, "INFO",
                         f"节点白名单生效：仅同步 {len(nodes)} 个指定文档")
                # 知识库名称映射（workspaceId → 名称，60s 缓存）：固化到文档行，
                # 供知识中心展示「钉钉+知识库名称」与跳转链接，避免列表多跳 join
                try:
                    ws_names = {ws.get("workspaceId"): ws.get("name", "")
                                for ws in dt.list_workspaces() if ws.get("workspaceId")}
                except Exception:  # noqa: BLE001 — 名称解析失败不阻断同步
                    ws_names = {}
                dataset_name = source.dify_dataset_name or f"文档库-{library_id}"
                remote = {n["nodeId"]: n for n in nodes if n.get("nodeId")}
                mappings = {m.node_id: m for m in
                            db.query(SyncDocumentMapping).filter_by(source_id=source.id).all()}

                # ① 删除：钉钉侧已不存在且策略为 sync（照旧立即执行，不进批处理）
                deleted = 0
                for node_id, mapping in list(mappings.items()):
                    if node_id in remote:
                        continue
                    if source.delete_policy == "sync":
                        task = SyncTask(
                            run_id=run_id, source_id=source.id, kind="sync", action="delete",
                            node_id=node_id, name=(mapping.name or node_id)[:500],
                            file_ext=Path(mapping.name or "").suffix.lower().lstrip("."),
                            dataset_id=str(library_id), dataset_name=dataset_name,
                            status="running", started_at=datetime.utcnow(),
                            trigger=run.trigger, operator=run.operator)
                        db.add(task)
                        db.commit()
                        try:
                            if mapping.dify_document_id:
                                backend.delete_document(str(library_id), mapping.dify_document_id)
                            db.delete(mapping)
                            deleted += 1
                            task.status = "success"
                            task.finished_at = datetime.utcnow()
                            _log(db, run_id, "INFO", f"删除已同步文档: {mapping.name}")
                        except Exception as exc:  # noqa: BLE001
                            task.status = "failed"
                            task.finished_at = datetime.utcnow()
                            task.error = f"删除失败: {exc}"[:2000]
                            _log(db, run_id, "ERROR", f"删除失败 [{mapping.name}]: {exc}")
                        db.commit()
                    else:
                        _log(db, run_id, "INFO",
                             f"钉钉侧已删除但策略为 keep，保留文档: {mapping.name}")

                # ② 预建：任务进同步队列、文档进列表（PENDING 占位），一次 commit 落库
                queued = skipped = 0
                for node in nodes:
                    node_id = node.get("nodeId", "")
                    name = node.get("name", node_id) or node_id
                    if not node_id or skip_reason(node, s, "library"):
                        skipped += 1
                        continue
                    mapping = mappings.get(node_id)
                    if mapping is not None and not mapping.enabled:
                        skipped += 1
                        continue
                    # 增量：已同步且节点元数据指纹未变 → 不重建任务。
                    # 无时间戳节点 meta_hash 为空串，由批处理下载后按 content_hash 判定跳过。
                    if (mapping is not None and mapping.status == "synced"
                            and mapping.meta_hash
                            and _meta_hash(node) == mapping.meta_hash):
                        skipped += 1
                        continue
                    # action 判定依据 mapping 上轮状态（编目会提前写 dify_document_id，
                    # 不能沿用直跑模式「有 document_id 即 update」的口径）
                    was_synced = mapping is not None and mapping.status == "synced"
                    if mapping is None:
                        mapping = SyncDocumentMapping(
                            source_id=source.id, node_id=node_id, name=name,
                            category=node.get("category", ""), status="pending")
                        db.add(mapping)
                    # 复用已存在的文档行（重同步 / 用户手删后重建）；缺失则预建新行
                    doc = None
                    if mapping.dify_document_id:
                        try:
                            doc = db.query(LibraryDocument).filter_by(
                                id=uuid.UUID(mapping.dify_document_id),
                                library_id=library_id).first()
                        except ValueError:
                            doc = None
                    if doc is None:
                        doc_uuid = uuid.uuid4()
                        db.add(LibraryDocument(
                            id=doc_uuid, library_id=library_id, name=name, source="dingtalk",
                            storage_path="", size=0, status="PENDING",
                            progress=0, message="待同步", chunk_count=0,
                            source_url=f"https://alidocs.dingtalk.com/i/nodes/{node_id}" if node_id else None,
                            source_workspace_name=ws_names.get(node.get("workspaceId", "")) or None,
                            updated_by=run.operator or None))
                        mapping.dify_document_id = str(doc_uuid)
                    else:
                        doc.status = "PENDING"
                        doc.progress = 0
                        doc.message = "待同步"
                        if node_id:
                            doc.source_url = f"https://alidocs.dingtalk.com/i/nodes/{node_id}"
                        if ws_names.get(node.get("workspaceId", "")):
                            doc.source_workspace_name = ws_names[node.get("workspaceId", "")]
                        doc.updated_by = run.operator or doc.updated_by
                    mapping.name = name
                    mapping.status = "pending"
                    mapping.error = ""
                    db.add(SyncTask(
                        run_id=run_id, source_id=source.id, kind="sync",
                        action=("update" if was_synced else "create"),
                        node_id=node_id, name=name[:500],
                        file_ext=node_extension(node),
                        dataset_id=str(library_id), dataset_name=dataset_name,
                        status="pending", trigger=run.trigger, operator=run.operator,
                        library_document_id=mapping.dify_document_id))
                    queued += 1
                db.commit()

                run.total = queued + deleted
                run.deleted_count = deleted
                if queued == 0 and deleted == 0:
                    run.status = "success"
                    run.finished_at = datetime.utcnow()
                    run.message = f"编目完成：无可同步内容（total={run.total} skipped={skipped}）"
                    db.commit()
                    return {"run_id": run_id, "status": run.status, "total": run.total,
                            "queued": queued, "deleted": deleted, "skipped": skipped,
                            "message": run.message}
                run.message = (f"编目完成：{queued} 篇文档已排队待同步"
                               f"（删除 {deleted}，跳过 {skipped}），由批处理任务分批消费")
                db.commit()
                _log(db, run_id, "INFO", run.message)

                # 立即触发一次批处理（best effort：失败时 30s 内 cron 自然接管）
                try:
                    from .scheduler import trigger_batch_once
                    trigger_batch_once()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("触发批处理作业失败（等待 cron 接管）: %s", exc)
                # 编目动作本身成功（run 保持 running，等批处理分批消费后收尾）
                return {"run_id": run_id, "status": "success", "total": run.total,
                        "queued": queued, "deleted": deleted, "skipped": skipped,
                        "message": run.message}
            finally:
                dt.close()
                backend.close()
        except Exception as exc:  # noqa: BLE001
            logger.exception("编目失败 source_id=%s", source_id)
            db.rollback()
            if run_id is not None:
                from .runtime import finish_interrupted
                finish_interrupted(run_id, f"编目异常终止: {exc}")
            return {"run_id": run_id, "status": "failed", "message": str(exc)}
        finally:
            db.close()
