"""独立进程执行同步，硬超时终止整个进程组；PG 锁避免多进程重复同步。"""
from contextlib import contextmanager
from datetime import datetime
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys

from sqlalchemy import text

from kb_common.config import get_settings
from kb_common.models import SyncLog, SyncRun, SyncSource, SyncTask
from .sync_database import SyncSessionLocal, sync_engine

logger = logging.getLogger(__name__)
SUPERVISOR_LOCK = 73101
WORKER_LOCK = 73102


@contextmanager
def source_lock(source_id: int, namespace: int):
    # Session-level advisory locks must be released before returning to the pool.
    with sync_engine.connect() as conn:
        acquired = conn.execute(text('SELECT pg_try_advisory_lock(:ns, :id)'),
                                {'ns': namespace, 'id': source_id}).scalar()
        conn.commit()
        try:
            yield bool(acquired)
        finally:
            if acquired:
                conn.execute(text('SELECT pg_advisory_unlock(:ns, :id)'),
                             {'ns': namespace, 'id': source_id})
                conn.commit()


def finish_interrupted(run_id: int, reason: str) -> None:
    # A fresh transaction also works after a worker's DB transaction has failed.
    # 超时/崩溃/收编孤儿时收尾：中断前已完成同步的文档保留成果——
    # 有成功项 → partial（部分成功）+ WARNING 日志；否则 failed + ERROR 日志。
    with SyncSessionLocal() as db:
        run = db.query(SyncRun).filter_by(id=run_id, status='running').with_for_update().first()
        if run is None:
            return
        succeeded = (run.created_count or 0) + (run.updated_count or 0) + (run.deleted_count or 0)
        failed = run.failed_count or 0
        run.finished_at = datetime.utcnow()
        if succeeded > 0:
            run.status = 'partial'
            summary = f"{reason}；已部分同步：成功 {succeeded} 个，失败 {failed} 个，其余未同步"
            level = 'WARNING'
        else:
            run.status = 'failed'
            summary = f"{reason}；成功 0 个，失败 {failed} 个"
            level = 'ERROR'
        run.message = summary
        db.add(SyncLog(run_id=run.id, level=level, message=summary))
        # 同步队列：未开始/进行中的任务随运行中断一并标记失败，可任务级重试。
        db.query(SyncTask).filter(
            SyncTask.run_id == run_id,
            SyncTask.status.in_(['pending', 'running']),
        ).update({'status': 'failed', 'finished_at': datetime.utcnow(),
                  'error': reason[:2000]}, synchronize_session=False)
        db.commit()


def _recover_source(source_id: int) -> None:
    with SyncSessionLocal() as db:
        ids = [r.id for r in db.query(SyncRun.id).filter_by(source_id=source_id, status='running')]
    for run_id in ids:
        finish_interrupted(run_id, '同步执行进程已中断，任务已标记失败，可重新同步')


def recover_interrupted_runs() -> None:
    """收编孤儿 running run（30s 周期扫描）。

    batch run（library 编目预建任务的 run）跨批次长驻 running 是健康状态，
    仅当长时间无推进（批处理作业死亡/被停）才按 sync_batch_stale_run_seconds 收编；
    直跑模式（dify/ragflow）保持原 per-source 双锁收编逻辑。
    """
    from datetime import timedelta

    with SyncSessionLocal() as db:
        # 派生判定 batch run：存在携带 library_document_id 的任务（无迁移、无新列）
        batch_run_ids = {r[0] for r in db.query(SyncTask.run_id).filter(
            SyncTask.library_document_id.isnot(None),
            SyncTask.run_id.isnot(None)).distinct()}
        stale_before = datetime.utcnow() - timedelta(
            seconds=get_settings().sync_batch_stale_run_seconds)
        rows = db.query(SyncRun.id, SyncRun.source_id, SyncRun.started_at).filter_by(
            status='running').all()
    for run_id, source_id, started_at in rows:
        if run_id in batch_run_ids:
            if started_at and started_at < stale_before:
                finish_interrupted(run_id, '批处理长时间未推进（批处理作业可能已停止），任务已标记失败，可重新同步')
            continue
        with source_lock(source_id, SUPERVISOR_LOCK) as supervisor_free:
            if not supervisor_free:
                continue
            with source_lock(source_id, WORKER_LOCK) as worker_free:
                if worker_free:
                    _recover_source(source_id)


def kill_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_sync(source_id: int, trigger: str = 'manual', operator: str = '') -> dict:
    with source_lock(source_id, SUPERVISOR_LOCK) as acquired:
        if not acquired:
            return {'status': 'skipped', 'message': '该同步源正在运行'}
        # An orphan worker may outlive its supervisor until its own hard deadline.
        with source_lock(source_id, WORKER_LOCK) as worker_free:
            if not worker_free:
                return {'status': 'skipped', 'message': '该同步源正在运行'}
            _recover_source(source_id)
            with SyncSessionLocal() as db:
                source = db.get(SyncSource, source_id)
                if source is None or not source.enabled:
                    return {'status': 'skipped', 'message': '同步源不存在或已停用'}
                run = SyncRun(source_id=source_id, trigger=trigger, status='running',
                              message='准备启动同步',
                              operator=operator or ('系统' if trigger == 'schedule' else ''))
                db.add(run)
                db.commit()
                run_id = run.id
        timeout = get_settings().sync_run_timeout_seconds
        process = None
        try:
            process = subprocess.Popen(
                [sys.executable, '-m', 'app.services.sync.worker', str(run_id)],
                cwd=str(Path(__file__).resolve().parents[3]),
                start_new_session=True,
            )
            process.wait(timeout=timeout)
            finish_interrupted(run_id, f'同步进程异常退出（退出码 {process.returncode}），可重试')
        except subprocess.TimeoutExpired:
            kill_process_group(process)
            finish_interrupted(run_id, f'同步超时（超过 {timeout} 秒），已终止本地同步进程；已提交的 Dify 索引可能继续，可重试')
        except BaseException as exc:
            if process is not None:
                kill_process_group(process)
            finish_interrupted(run_id, f'同步执行中断: {type(exc).__name__}: {exc}')
            raise
        with SyncSessionLocal() as db:
            run = db.get(SyncRun, run_id)
            from .engine import SyncEngine
            return SyncEngine._summary(run)
