"""基于 XXL-Job 的定时同步调度（完全替换内置 APScheduler）。

职责划分：
- 本平台同步源为作业配置唯一事实源：cron 存 sync_sources.cron（5 段 crontab），
  新建/编辑/启停/删除时经 admin API 幂等同步到 XXL-Job（推送时转 Quartz 6 段）；
- 调度与触发由 XXL-Job admin 负责；内嵌执行器（xxljob_executor）接收回调跑 run_sync；
- 中断运行收编扫描（超时/崩溃孤儿 run）为独立线程，与调度中心无关。

启动行为（fail fast）：
- XXL_JOB_ENABLED=true（默认）时 admin 不可达/登录失败 → 抛异常终止启动；
- XXL_JOB_ENABLED=false → 定时同步整体关闭（手动同步/同步队列/重试不受影响），启动告警。
"""
from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from kb_common.config import get_settings
from kb_common.models import SyncSource

from .engine import run_sync
from .runtime import recover_interrupted_runs
from .sync_database import SyncSessionLocal
from .xxljob_admin import XxlJobAdminClient, XxlJobAdminError, crontab_to_quartz
from .xxljob_executor import XxlJobExecutor, detect_ip

logger = logging.getLogger(__name__)

# 调度中心未启用时的进程内直跑通道（仅 XXL_JOB_ENABLED=false 的开发/降级场景）
_direct_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sync-direct")

_admin: XxlJobAdminClient | None = None
_executor: XxlJobExecutor | None = None
_group_id: int | None = None
_recovery_stop = threading.Event()
_recovery_thread: threading.Thread | None = None


def get_admin_client() -> XxlJobAdminClient:
    global _admin
    if _admin is None:
        s = get_settings()
        _admin = XxlJobAdminClient(s.xxl_job_admin_url, s.xxl_job_admin_user,
                                   s.xxl_job_admin_password, s.xxl_job_access_token)
    return _admin


def _ensure_group() -> int:
    global _group_id
    if _group_id is None:
        s = get_settings()
        _group_id = get_admin_client().ensure_group(
            s.xxl_job_executor_appname, "知识治理专家同步执行器")
    return _group_id


def _job_spec(group_id: int, source: SyncSource) -> dict:
    return {
        "jobGroup": group_id,
        "jobDesc": f"知识同步-{source.name}",
        "author": "kge",
        "alarmEmail": "",
        "scheduleType": "CRON",
        "scheduleConf": crontab_to_quartz(source.cron),
        "glueType": "BEAN",
        "executorHandler": "syncSource",
        "executorParam": str(source.id),
        "executorRouteStrategy": "FIRST",
        "childJobId": "",
        "misfireStrategy": "DO_NOTHING",
        "executorBlockStrategy": "SERIAL_EXECUTION",
        "executorTimeout": 0,
        "executorFailRetryCount": 0,
        "glueRemark": "GLUE代码初始化描述",
    }


def _source_backend_type(source_id: int) -> str:
    """查询同步源 backend_type（查询失败返回空串，走默认直跑分支）。"""
    try:
        with SyncSessionLocal() as db:
            source = db.get(SyncSource, source_id)
            return (source.backend_type or "").strip().lower() if source else ""
    except Exception:  # noqa: BLE001
        return ""


def _handle_sync_source(param: str, job_log) -> tuple[int, str]:
    """xxl-job handler：param 为同步源 ID（cron 触发）或 JSON（手动触发携带 operator/trigger）。

    library 源分流到编目（run_catalog：预建任务/文档，由 syncBatch 批处理分批消费）；
    dify/ragflow 源维持整源直跑（run_sync）。
    """
    source_id: int | None = None
    operator = ""
    trigger = "schedule"
    raw = (param or "").strip()
    if raw:
        try:
            data = json.loads(raw)
            source_id = int(data.get("source_id"))
            operator = str(data.get("operator") or "")
            trigger = str(data.get("trigger") or "manual")
        except (ValueError, TypeError, AttributeError):
            try:
                source_id = int(raw)
            except ValueError:
                return 500, f"无效的作业参数（应为同步源 ID 或 JSON）: {param!r}"
    if source_id is None:
        return 500, f"无效的作业参数（应为同步源 ID 或 JSON）: {param!r}"
    if _source_backend_type(source_id) == "library":
        from .catalog import run_catalog
        summary = run_catalog(source_id, trigger, operator)
    else:
        summary = run_sync(source_id, trigger, operator)
    job_log.log(f"同步结果: {summary}")
    status = summary.get("status")
    message = str(summary.get("message") or status)[:400]
    if status in ("success", "partial", "skipped"):
        return 200, message
    return 500, message


def _handle_sync_batch(param: str, job_log) -> tuple[int, str]:
    """xxl-job handler：全局批处理 tick（library 编目任务分批消费，param 未用）。"""
    from .batch import run_sync_batch_tick
    return run_sync_batch_tick(job_log)


def _apply_source_job(admin: XxlJobAdminClient, group_id: int, source: SyncSource) -> int | None:
    """幂等应用单个同步源的作业配置；返回处于启动状态的作业 ID（停用/跳过/失败为 None）。"""
    job_id = source.xxl_job_id
    if not source.enabled:
        if job_id:
            try:
                admin.stop_job(job_id)
            except XxlJobAdminError as exc:
                logger.warning("停用 XXL-Job 作业 %s 失败: %s", job_id, exc)
        return None
    try:
        spec = _job_spec(group_id, source)
    except ValueError as exc:
        # cron 转不了 Quartz：停掉旧作业，避免旧 cron 继续跑（路由层保存时已校验）
        logger.error("同步源 %s 的 cron 转换失败，跳过并停用旧作业: %s", source.id, exc)
        if job_id:
            try:
                admin.stop_job(job_id)
            except XxlJobAdminError:
                pass
        return None
    try:
        if job_id:
            try:
                admin.update_job({**spec, "id": job_id})
            except XxlJobAdminError:
                logger.warning("XXL-Job 作业 %s 在调度中心不存在，重建", job_id)
                job_id = None
        if not job_id:
            job_id = admin.add_job(spec)
            source.xxl_job_id = job_id
        admin.start_job(job_id)
        return job_id
    except XxlJobAdminError as exc:
        logger.error("同步源 %s 的 XXL-Job 作业同步失败: %s", source.id, exc)
        return None


def sync_all_jobs() -> int:
    """把所有同步源的作业配置幂等同步到 admin；返回处于启动状态的作业数。"""
    admin = get_admin_client()
    group_id = _ensure_group()
    active = 0
    with SyncSessionLocal() as db:
        for source in db.query(SyncSource).order_by(SyncSource.id).all():
            if _apply_source_job(admin, group_id, source) is not None:
                active += 1
        db.commit()
    # 全局批处理作业（library 分批同步消费）一并幂等对齐
    _apply_batch_job(admin, group_id)
    return active


def _batch_schedule_conf() -> str:
    """批处理调度表达式：兼容 5 段 crontab（自动转 Quartz）与 Quartz 6 段（含秒位）。"""
    raw = (get_settings().sync_batch_cron or "").strip()
    if len(raw.split()) == 5:
        return crontab_to_quartz(raw)
    return raw


def _batch_job_spec(group_id: int) -> dict:
    return {
        "jobGroup": group_id,
        "jobDesc": "知识同步批处理（全局）",
        "author": "kge",
        "alarmEmail": "",
        "scheduleType": "CRON",
        "scheduleConf": _batch_schedule_conf(),
        "glueType": "BEAN",
        "executorHandler": "syncBatch",
        "executorParam": "",
        "executorRouteStrategy": "FIRST",
        "childJobId": "",
        "misfireStrategy": "DO_NOTHING",
        # 单批可能超过 tick 间隔（大文件下载/解析提交）：丢弃堆积触发，靠 idleBeat 回报 busy
        "executorBlockStrategy": "DISCARD_LATER",
        "executorTimeout": 0,
        "executorFailRetryCount": 0,
        "glueRemark": "GLUE代码初始化描述",
    }


_batch_job_id: int | None = None


def _apply_batch_job(admin: XxlJobAdminClient, group_id: int) -> int | None:
    """幂等注册/启动全局批处理作业（按 executorHandler 查找，作业 ID 不落库）。"""
    global _batch_job_id
    spec = _batch_job_spec(group_id)
    try:
        if _batch_job_id:
            try:
                admin.update_job({**spec, "id": _batch_job_id})
                admin.start_job(_batch_job_id)
                return _batch_job_id
            except XxlJobAdminError:
                logger.warning("批处理作业 %s 更新失败，按 handler 重新查找", _batch_job_id)
                _batch_job_id = None
        row = admin.find_job_by_handler("syncBatch")
        if row is not None:
            _batch_job_id = int(row["id"])
            admin.update_job({**spec, "id": _batch_job_id})
        else:
            _batch_job_id = admin.add_job(spec)
        admin.start_job(_batch_job_id)
        return _batch_job_id
    except XxlJobAdminError as exc:
        logger.error("批处理作业注册失败: %s", exc)
        _batch_job_id = None
        return None


def ensure_batch_job_running() -> int | None:
    """确保批处理作业已在 admin 注册并启动（编目触发批处理前调用）。"""
    if not get_settings().xxl_job_enabled:
        return None
    return _apply_batch_job(get_admin_client(), _ensure_group())


def trigger_batch_once() -> None:
    """立即触发一轮批处理（编目完成后调用，不等下一个 tick；best effort）。"""
    if not get_settings().xxl_job_enabled:
        from .batch import run_sync_batch_tick
        _direct_executor.submit(run_sync_batch_tick)
        return
    job_id = _batch_job_id or ensure_batch_job_running()
    if job_id:
        get_admin_client().trigger_job(job_id)


def trigger_source_sync(source_id: int, operator: str = "") -> dict:
    """「确认并开始同步」/立即同步：触发消息交给 XXL-Job admin，由执行器回调执行。

    不经 MQ：admin 的 /jobinfo/trigger 即可靠投递通道（触发留痕于调度日志，
    执行器串行队列缓冲并发）；执行器内嵌 kb-api，与 UI 同生命周期，无投递空窗。
    调度关闭（XXL_JOB_ENABLED=false）时回退进程内直跑，保证开发环境可用。
    """
    if not get_settings().xxl_job_enabled:
        # library 源降级也走编目（run_catalog 内部会触发进程内批处理线程）
        if _source_backend_type(source_id) == "library":
            from .catalog import run_catalog
            _direct_executor.submit(run_catalog, source_id, "manual", operator)
        else:
            from .engine import run_sync as direct_run
            _direct_executor.submit(direct_run, source_id, "manual", operator)
        return {"via": "direct",
                "message": "调度中心未启用（XXL_JOB_ENABLED=false），本次直接执行；进度可在同步队列查看"}
    admin = get_admin_client()
    with SyncSessionLocal() as db:
        source = db.get(SyncSource, source_id)
        if source is None:
            raise XxlJobAdminError(f"同步源 {source_id} 不存在")
        job_id = source.xxl_job_id
        if not job_id:
            # 作业缺失（如调度关闭期间建的源）：先对齐一次配置再触发
            if _apply_source_job(admin, _ensure_group(), source) is None:
                raise XxlJobAdminError("调度中心无可用作业（同步源已停用或 cron 无效），请检查同步源配置")
            db.commit()
            job_id = source.xxl_job_id
    param = json.dumps({"source_id": source_id, "operator": operator, "trigger": "manual"},
                       ensure_ascii=False)
    admin.trigger_job(job_id, param)
    return {"via": "xxl-job",
            "message": "同步任务已提交调度中心（XXL-Job），进度见同步队列，执行日志见调度控制台"}


def trigger_first_sync(source_id: int, operator: str = "") -> None:
    """同步源创建后的首次同步：进程内直跑，绕过 XXL-Job 往返。

    用户预期「创建后立即开始同步」，而 admin→执行器回调存在间歇性失败
    （开发态热重载窗口/网络抖动），首次同步不走调度往返、直跑最可靠；
    后续周期同步仍由 cron（XXL-Job）驱动。
    library 源直跑的是编目（预建任务/文档，立即入队进列表），实际内容
    由 syncBatch 批处理作业分批同步。
    """
    if _source_backend_type(source_id) == "library":
        from .catalog import run_catalog
        _direct_executor.submit(run_catalog, source_id, "manual", operator)
        return
    from .engine import run_sync as direct_run
    _direct_executor.submit(direct_run, source_id, "manual", operator)


def reload_sync_jobs() -> int:
    """同步源 CRUD 后调用：幂等全量对齐作业配置。调度关闭时为 no-op。"""
    if not get_settings().xxl_job_enabled:
        return 0
    return sync_all_jobs()


def remove_source_job(job_id: int | None) -> None:
    """删除同步源时清理调度中心作业（best effort）。"""
    if not job_id or not get_settings().xxl_job_enabled:
        return
    try:
        get_admin_client().remove_job(job_id)
    except XxlJobAdminError as exc:
        logger.warning("删除 XXL-Job 作业 %s 失败（请在控制台手工清理）: %s", job_id, exc)


def _recovery_loop() -> None:
    while not _recovery_stop.wait(30):
        try:
            recover_interrupted_runs()
        except Exception:  # noqa: BLE001
            logger.exception("中断运行收编扫描失败")


def _start_recovery_thread() -> None:
    global _recovery_thread
    if _recovery_thread is not None and _recovery_thread.is_alive():
        return
    _recovery_stop.clear()
    _recovery_thread = threading.Thread(target=_recovery_loop,
                                        name="sync-recover-interrupted", daemon=True)
    _recovery_thread.start()


# 降级批处理轮询：XXL_JOB_ENABLED=false / admin 不可达时由进程内线程兜底消费队列，
# 间隔与默认批处理 cron（每 30s）对齐。
_BATCH_FALLBACK_INTERVAL_SECONDS = 30
_batch_fallback_stop = threading.Event()
_batch_fallback_thread: threading.Thread | None = None


def _batch_fallback_loop() -> None:
    from .batch import run_sync_batch_tick
    while not _batch_fallback_stop.wait(_BATCH_FALLBACK_INTERVAL_SECONDS):
        try:
            run_sync_batch_tick()
        except Exception:  # noqa: BLE001
            logger.exception("降级批处理 tick 失败")


def _start_batch_fallback_thread() -> None:
    global _batch_fallback_thread
    if _batch_fallback_thread is not None and _batch_fallback_thread.is_alive():
        return
    _batch_fallback_stop.clear()
    _batch_fallback_thread = threading.Thread(target=_batch_fallback_loop,
                                              name="sync-batch-fallback", daemon=True)
    _batch_fallback_thread.start()


def start_sync_scheduler() -> int:
    """FastAPI 启动时调用。

    XXL-Job admin 不可达时**降级**（记录错误、跳过 executor 启动），
    绝不抛异常——lifespan 抛异常会直接杀死整个 kb-api（BGE-M3/搜索/问答全挂），
    且 reloader 模式下 worker 退出后端口仍在监听但无人 accept，所有请求挂死超时。
    cron 定时同步缺席不影响手动同步/同步队列/失败重试。
    """
    s = get_settings()
    _start_recovery_thread()
    if not s.xxl_job_enabled:
        logger.warning("XXL-Job 调度已禁用（XXL_JOB_ENABLED=false）："
                       "cron 定时同步不运行，手动同步/同步队列/重试不受影响；"
                       "批处理由进程内降级线程消费")
        _start_batch_fallback_thread()
        return 0
    admin = get_admin_client()
    try:
        admin.ping()
    except Exception as exc:  # noqa: BLE001 — 调度器缺席不能拖死 API 服务
        logger.error("XXL-Job admin 不可达（%s），本次启动跳过定时调度：cron 同步不运行，"
                     "手动同步/同步队列/重试不受影响；批处理由进程内降级线程消费。"
                     "恢复 admin 后重启 kb-api 即可恢复调度", exc)
        _start_batch_fallback_thread()
        return 0
    global _executor
    ip = s.xxl_job_executor_ip or detect_ip(s.xxl_job_admin_url)
    log_path = (Path(s.xxl_job_executor_log_path) if s.xxl_job_executor_log_path
                else Path(__file__).resolve().parents[3] / "data" / "xxljob-log")
    _executor = XxlJobExecutor(s.xxl_job_admin_url, s.xxl_job_access_token,
                               s.xxl_job_executor_appname, ip, s.xxl_job_executor_port,
                               log_path)
    _executor.start({"syncSource": _handle_sync_source, "syncBatch": _handle_sync_batch})
    count = sync_all_jobs()
    logger.info("XXL-Job 调度已启动：执行器 http://%s:%s，启用作业 %s 个（含批处理作业）",
                ip, s.xxl_job_executor_port, count)
    return count


def shutdown_sync_scheduler() -> None:
    _recovery_stop.set()
    _batch_fallback_stop.set()
    global _executor
    if _executor is not None:
        _executor.stop()
        _executor = None
