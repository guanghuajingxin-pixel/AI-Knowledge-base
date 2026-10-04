"""文档库（document-libraries）同步后端：实现与 DifyClient/RagflowSyncClient 相同的方法面。

SyncEngine 通过 make_backend() 获取后端实例，无需区分具体引擎。
本后端将钉钉文件写入本地文档库（MinIO + LibraryDocument + 自动解析）。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from kb_common.config import get_settings
from kb_common.models import LibraryDocument

logger = logging.getLogger(__name__)


class LibraryBackend:
    """文档库同步后端：上传文件到 MinIO → 创建 LibraryDocument → 触发解析。"""

    def __init__(self, library_id: int):
        self.library_id = library_id

    def resolve_dataset(self, dataset_id: str | None, dataset_name: str) -> dict:
        """文档库不需要远程解析：直接返回 library_id 作为 dataset_id。"""
        return {"id": str(self.library_id), "name": dataset_name}

    def upload_file(self, dataset_id: str, local_file: Path, *,
                    file_name: str = "", doc_id: str | None = None) -> dict:
        """上传文件到文档库：MinIO 存储 + 创建/更新文档记录 + 触发解析。

        doc_id 给定时更新批处理预建的 LibraryDocument 行（编目阶段已 PENDING 占位），
        并非阻塞提交解析（批处理 tick 每轮 refresh 兜底收割）；
        为 None 时保持旧行为（新建 uuid + 阻塞等待解析完成），兼容 legacy 直跑/重试路径。
        """
        name = file_name or local_file.name
        content = local_file.read_bytes()
        from kb_common.clients import minio_client
        from .sync_database import SyncSessionLocal

        if doc_id:
            target_id = uuid.UUID(doc_id)
            key = f"document-libraries/{self.library_id}/{target_id}/{name}"
            with SyncSessionLocal() as db:
                row = db.query(LibraryDocument).filter_by(
                    id=target_id, library_id=self.library_id
                ).with_for_update().first()
                if row is None:
                    raise RuntimeError(f"目标文档 {doc_id} 不存在（可能已被删除），无法回写同步内容")
                minio_client.upload_bytes(minio_client.RAW, key, content)
                row.name = name
                row.storage_path = key
                row.size = len(content)
                row.status = "UPLOADED"
                row.progress = 0
                row.message = ""
                db.commit()
            # 批处理模式：只提交解析任务，不等待完成
            self._submit_parse(target_id)
            return {"document": {"id": str(target_id)}, "batch": ""}

        new_id = uuid.uuid4()
        key = f"document-libraries/{self.library_id}/{new_id}/{name}"
        minio_client.upload_bytes(minio_client.RAW, key, content)

        # 使用同步 session 创建文档记录（source=dingtalk：钉钉同步来源，与本地上传区分）
        with SyncSessionLocal() as db:
            doc = LibraryDocument(
                id=new_id, library_id=self.library_id, name=name, source="dingtalk",
                storage_path=key, size=len(content),
                status="UPLOADED", progress=0, message="", chunk_count=0,
            )
            db.add(doc)
            db.commit()

        # 触发异步解析
        self._trigger_parse(new_id)

        return {"document": {"id": str(new_id)}, "batch": ""}

    def update_file(self, dataset_id: str, doc_id: str, local_file: Path, *, file_name: str = "") -> dict:
        """更新文档：删除旧版本 → 重新上传。"""
        # 删除旧文档
        self.delete_document(dataset_id, doc_id)
        # 重新上传
        return self.upload_file(dataset_id, local_file, file_name=file_name)

    def delete_document(self, dataset_id: str, doc_id: str) -> None:
        """删除文档库中的文档；级联取消该文档关联的排队同步任务。"""
        from datetime import datetime

        from kb_common.models import SyncTask

        from .sync_database import SyncSessionLocal
        with SyncSessionLocal() as db:
            doc = db.query(LibraryDocument).filter_by(
                id=uuid.UUID(doc_id), library_id=self.library_id
            ).first()
            # 级联取消批处理模式下预建的 pending 任务（文档删除后无需再同步）
            db.query(SyncTask).filter(
                SyncTask.library_document_id == str(doc_id),
                SyncTask.status == "pending",
            ).update({"status": "failed", "finished_at": datetime.utcnow(),
                      "error": "目标文档已删除，任务取消"}, synchronize_session=False)
            if doc:
                # 删除 MinIO 文件
                try:
                    from kb_common.clients import minio_client
                    minio_client.minio.remove_object(minio_client.RAW, doc.storage_path)
                except Exception:
                    pass
                db.delete(doc)
            db.commit()

    def wait_indexing(self, dataset_id: str, batch: str, timeout: float, **kwargs) -> str:
        """文档库解析是异步的，不阻塞等待。"""
        return "submitted"

    def local_file_node_id(self, dataset_id: str) -> str:
        """文档库不需要流水线节点。"""
        return ""

    def close(self) -> None:
        pass

    def _trigger_parse(self, doc_id: uuid.UUID) -> None:
        """触发文档解析并等待完成（同步进程内无前端轮询，必须主动收割）。"""
        try:
            asyncio.run(self._async_parse_and_wait(doc_id))
        except Exception as e:
            logger.warning("文档解析失败 %s: %s", doc_id, e)

    def _submit_parse(self, doc_id: uuid.UUID) -> None:
        """非阻塞提交解析（批处理模式）：只提交 MinerU job，不等待完成。

        解析状态由批处理 tick 每轮 refresh 兜底收割（不依赖前端轮询）；
        解析提交失败向上抛出，由批处理把任务标 failed。
        """
        asyncio.run(self._async_submit_parse(doc_id))

    async def _async_submit_parse(self, doc_id: uuid.UUID) -> None:
        # 独立短命引擎：批处理线程的临时事件循环不能复用绑定主 loop 的全局连接池
        from kb_common.database import fresh_session

        from app.routes.managed_library import _do_parse
        async with fresh_session() as s:
            await _do_parse(s, self.library_id, doc_id)
            await s.commit()

    async def _async_parse_and_wait(self, doc_id: uuid.UUID) -> None:
        """触发解析并轮询直到完成/失败（复用 managed_library 的状态机）。"""
        import time
        from kb_common.database import SessionLocal
        from app.routes.managed_library import _do_parse, refresh_state, engine

        timeout = float(get_settings().sync_indexing_timeout_seconds or 600)
        async with SessionLocal() as s:
            await _do_parse(s, self.library_id, doc_id)
            await s.commit()

        adapter = engine()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            async with SessionLocal() as s:
                from sqlalchemy import select
                doc = (await s.execute(
                    select(LibraryDocument).where(LibraryDocument.id == doc_id)
                )).scalar_one_or_none()
                if doc is None:
                    return
                if doc.status in ("COMPLETED", "FAILED", "CANCELLED"):
                    if doc.status != "COMPLETED":
                        raise RuntimeError(f"文档解析{doc.status}: {doc.message or ''}")
                    return
                lib = None
                await refresh_state(adapter, s, lib, doc)
                await s.commit()
        raise RuntimeError(f"文档解析超时（{int(timeout)}s）")
