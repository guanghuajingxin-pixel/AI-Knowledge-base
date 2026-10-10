"""运营看板路由。

统计与持久化策略（流水表 operate_metric_records）：
- 知识存储总量 / 知识数量（含分布）/ 热门知识 Top20：每日 0 点定时统计（kb-api 内置调度器），
  或点「刷新」手动触发；每个指标统计完成即写入一条流水，展示时取各指标最新一条记录。
- 服务重启后从流水表恢复最新数据；若当天尚无记录（如 0 点时服务宕机）则自动补统计。
- 知识召回数 / 调用量：待知识采集流程打通、Dify 文档关联钉钉文档 ID 后统计（前端显示"即将发布"）。
"""
import asyncio
import logging
import time
from datetime import datetime, date, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select, delete
from sqlalchemy import func

from kb_common.database import SessionLocal
from app.deps import require_role

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/operate", tags=["operate"])

# ===== 配置 =====
_HOT_CATEGORIES = {"DOCUMENT", "ALIDOC"}   # 热门统计只算知识文档（上传文档 + 在线文档）
_RECORD_RETENTION_DAYS = 180               # 流水保留天数
_HOT_SCAN_CONCURRENCY = 3                  # 热门扫描并发 worker 数（钉钉限流约束，再高只会换来 403 重试）

# 遍历原始数据缓存（供热门扫描取候选清单；TTL 1 小时）
_stats_cache: dict = {"data": None, "expire_at": 0.0, "error": None, "walk_info": None}
# 热门扫描实时进度（进行中时供接口读取；完成后快照进 _snapshot 并落库）
_hot_cache: dict = {"scores": {}, "scanned": 0, "total": 0}
# 各指标最新统计结果（内存镜像 = 流水表最新记录；重启后由 _hydrate_snapshot 恢复）
_snapshot: dict = {"storage": None, "knowledge_count": None, "hot_docs": None, "loaded": False}
# 全量统计任务状态（phase: storage → walk → hot）
_job: dict = {"running": False, "phase": None}
_scheduler_started = False


def _gb(b: int) -> str:
    return f"{b / 1024 / 1024 / 1024:.2f}"


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _today() -> str:
    return date.today().isoformat()


# ===== 流水表读写 =====
async def _save_metric_record(metric_key: str, value: dict, trigger: str) -> None:
    """写入一条指标流水，并清理过期流水。"""
    from kb_common.models import OperateMetricRecord
    value = {**value, "metric_date": _today()}
    async with SessionLocal() as s:
        s.add(OperateMetricRecord(metric_key=metric_key, metric_date=date.today(),
                                  value=value, trigger=trigger))
        cutoff = date.today() - timedelta(days=_RECORD_RETENTION_DAYS)
        await s.execute(delete(OperateMetricRecord).where(OperateMetricRecord.metric_date < cutoff))
        await s.commit()


async def _load_latest_record(metric_key: str) -> dict | None:
    """取某指标最新一条流水。"""
    from kb_common.models import OperateMetricRecord
    async with SessionLocal() as s:
        row = (await s.execute(
            select(OperateMetricRecord)
            .where(OperateMetricRecord.metric_key == metric_key)
            .order_by(OperateMetricRecord.id.desc())
            .limit(1))).scalar_one_or_none()
        return row.value if row else None


async def _hydrate_snapshot() -> None:
    """服务启动后首次访问时，从流水表恢复各指标最新记录到内存快照。"""
    if _snapshot["loaded"]:
        return
    _snapshot["loaded"] = True
    for key in ("storage", "knowledge_count", "hot_docs"):
        try:
            val = await _load_latest_record(key)
        except Exception as e:
            logger.warning("读取运营指标流水失败(%s): %s", key, e)
            val = None
        if val:
            _snapshot[key] = val
    logger.info("运营指标快照已从流水表恢复：%s",
                [k for k in ("storage", "knowledge_count", "hot_docs") if _snapshot.get(k)])


def _is_stale() -> bool:
    """任一指标今天还没有统计记录 → 视为过期（需要重新统计）。"""
    for key in ("storage", "knowledge_count", "hot_docs"):
        v = _snapshot.get(key)
        if not v or v.get("metric_date") != _today():
            return True
    return False


# ===== 全量统计任务 =====
def _start_job(trigger: str, include_hot: bool = True) -> bool:
    """启动全量统计（已有任务在跑则忽略）。

    include_hot=False 时跳过「热门知识 Top20」扫描：该阶段要逐个文档 fork 一个
    dws CLI 子进程（实测 11037 个文档、约 25 分钟），期间整机 CPU 被打满、
    所有接口间歇性 3~50 秒——只能由每日 0 点定时任务和用户手动刷新触发，
    不能因为「服务重启补统计」或「有人打开运营页」就跑。
    """
    if _job["running"]:
        return False
    _job["running"] = True
    _job["phase"] = "storage"
    asyncio.create_task(_run_full_stats(trigger, include_hot=include_hot))
    return True


async def _run_full_stats(trigger: str, include_hot: bool = True) -> None:
    """全量统计：存储总量 → 知识数量/分布（全量遍历）→ 热门 Top20，各完成即落库并更新快照。"""
    from kb_common.clients import dingtalk_client as dt
    try:
        await dt.sync_runtime_config()
        if not dt.is_configured():
            logger.info("钉钉未配置，跳过运营指标统计")
            return

        # ---- 1) 存储总量（企业存储 API → CLI 兜底，秒级） ----
        sv = {"value": None, "bytes": None, "fallback": False, "error": None}
        try:
            b = await dt.get_org_storage_used()
            sv["bytes"] = b
            sv["value"] = _gb(b)
        except Exception as e:
            sv["error"] = str(e)
            logger.warning("存储总量获取失败: %s", e)
        if sv["value"]:
            await _save_metric_record("storage", sv, trigger)
            _snapshot["storage"] = {**sv, "created_at": _now_str()}

        # ---- 2) 知识数量 + 企业分布（全量遍历，约 30 分钟） ----
        _job["phase"] = "walk"
        data = None
        try:
            data = await dt.get_all_workspaces_stats()
            _stats_cache["data"] = data
            _stats_cache["walk_info"] = dt.get_last_walk_info()
            _stats_cache["expire_at"] = time.time() + 3600
            _stats_cache["error"] = None
            logger.info("钉钉知识库遍历完成：%d 个知识库", len(data))
        except Exception as e:
            _stats_cache["error"] = str(e)
            logger.warning("钉钉知识库遍历失败: %s", e)

        if data:
            # 无存储权限时回退为上传文件大小累计（在线文档 size=0，偏小）
            if sv["error"] and not sv["value"]:
                fb_bytes = sum(w.get("total_size", 0) for w in data)
                sv.update(bytes=fb_bytes, value=_gb(fb_bytes), fallback=True)

            kc = {
                "value": sum(w.get("file_count", 0) for w in data),
                "month_new": _count_month_new(data),
                "distribution": [
                    {"name": w.get("name", "未命名"), "value": w.get("file_count", 0),
                     "workspace_id": w.get("workspace_id")}
                    for w in sorted(data, key=lambda x: x.get("file_count", 0), reverse=True)
                ],
            }
            wi = _stats_cache.get("walk_info") or {}
            if wi.get("failed_folders"):
                kc["partial"] = True
                kc["failed_folders"] = wi["failed_folders"]
            await _save_metric_record("knowledge_count", kc, trigger)
            _snapshot["knowledge_count"] = {**kc, "created_at": _now_str()}

        # 存储失败时：回退尝试后落库（记录 error 供前端提示；成功时前面已落过）
        if not sv["value"]:
            await _save_metric_record("storage", sv, trigger)
            _snapshot["storage"] = {**sv, "created_at": _now_str()}

        # ---- 3) 热门知识 Top20（逐文档 fork dws CLI 统计，万级文档约 25 分钟） ----
        # 只在每日定时任务与手动刷新时跑：启动补统计/打开运营页都不触发，
        # 否则整机 CPU 被子进程吃满，整站点击都会卡几秒到几十秒。
        if data and include_hot:
            _job["phase"] = "hot"
            await _scan_hot_docs(trigger)
        elif data:
            logger.info("本次统计跳过热门知识扫描（trigger=%s），沿用上一次落库结果", trigger)

        logger.info("运营指标全量统计完成（trigger=%s）", trigger)
    except Exception as e:
        logger.warning("运营指标全量统计失败: %s", e)
    finally:
        _job["running"] = False
        _job["phase"] = None


def _parse_dingtalk_time(v) -> datetime | None:
    """解析钉钉时间字段并转为本地时区。

    钉钉 wiki 节点 API 实测返回 UTC 无秒 ISO 串（如 2026-07-08T18:08Z），
    兼容毫秒时间戳与带秒/时区偏移的 ISO 串；解析失败返回 None。
    """
    if v in (None, ""):
        return None
    s = str(v).strip()
    if s.isdigit():  # 毫秒时间戳
        try:
            return datetime.fromtimestamp(int(s) / 1000).astimezone()
        except (ValueError, OverflowError, OSError):
            return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone()  # 无时区信息的串按本地时间解释，统一返回本地时区 aware


def _count_month_new(data: list[dict]) -> int:
    """本月新增：按文件创建时间统计（缺失时回退修改时间），跨月界以本地时区为准。"""
    month_start = datetime.now().astimezone().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    n = 0
    for w in data:
        for f in w.get("files", []):
            t = (_parse_dingtalk_time(f.get("created_time"))
                 or _parse_dingtalk_time(f.get("modified_time")))
            if t and t >= month_start:
                n += 1
    return n


async def _scan_hot_docs(trigger: str) -> None:
    """并发调用 dws drive stats 逐文档统计访问/阅读数，完成后落库。"""
    from kb_common.clients import dingtalk_client as dt
    candidates = []
    for w in (_stats_cache["data"] or []):
        for f in w.get("files", []):
            if (f.get("category") or "") in _HOT_CATEGORIES and f.get("node_id"):
                candidates.append(f)
    _hot_cache["total"] = len(candidates)
    _hot_cache["scores"] = {}
    _hot_cache["scanned"] = 0

    # 固定 3 个 worker 从队列取任务：原先一次性 gather 上万个协程
    # （实测 11037 个文档 = 11037 个协程同时挂在事件循环上），内存与调度开销都很大。
    # 并发度仍为 3——受钉钉侧限流约束，再高只会换来 403 重试。
    queue: asyncio.Queue = asyncio.Queue()
    for f in candidates:
        queue.put_nowait(f)

    async def worker() -> None:
        while True:
            try:
                f = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                st = await dt.get_node_stats_via_cli(f["node_id"])
                _hot_cache["scores"][f["node_id"]] = st
                _hot_cache["scanned"] += 1
                if _hot_cache["scanned"] % 500 == 0:
                    logger.info("热门知识扫描进度：%d/%d", _hot_cache["scanned"], _hot_cache["total"])
            except Exception:
                pass  # 单节点失败跳过，不中断整体

    await asyncio.gather(*(worker() for _ in range(_HOT_SCAN_CONCURRENCY)))
    hd: dict = {"items": _top_hot_docs(20), "total": _hot_cache["total"],
                "scanned": _hot_cache["scanned"]}
    if _hot_cache["scanned"] < _hot_cache["total"]:
        hd["partial"] = True
    await _save_metric_record("hot_docs", hd, trigger)
    _snapshot["hot_docs"] = {**hd, "created_at": _now_str()}
    logger.info("热门知识统计完成：%d/%d 个文档", _hot_cache["scanned"], _hot_cache["total"])


def _top_hot_docs(n: int = 20) -> list[dict]:
    """按访问次数（查看）倒序取 Top N，访问数相同按阅读数排序。"""
    ranked = []
    for w in (_stats_cache["data"] or []):
        for f in w.get("files", []):
            st = _hot_cache["scores"].get(f.get("node_id"))
            if st:
                ranked.append({
                    "node_id": f.get("node_id"),
                    "title": f.get("name", ""),
                    "workspace": w.get("name", "未命名"),
                    "url": f.get("url") or "",
                    "read_count": st.get("read_count", 0),
                    "count": st.get("visit_count", 0),
                })
    ranked.sort(key=lambda x: (x["count"], x["read_count"]), reverse=True)
    out = []
    for i, r in enumerate(ranked[:n], 1):
        r["rank"] = i
        out.append(r)
    return out


# ===== 每日 0 点定时调度 =====
async def _daily_scheduler() -> None:
    while True:
        now = datetime.now()
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=10, microsecond=0)
        await asyncio.sleep(max(1.0, (nxt - now).total_seconds()))
        logger.info("每日 0 点定时统计启动")
        _start_job("daily")


def start_operate_scheduler() -> None:
    """FastAPI 启动时调用：恢复快照 + 缺今日数据则补统计 + 注册每日 0 点任务。"""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    async def _bootstrap() -> None:
        await asyncio.sleep(3)  # 等 DB / 事件循环就绪
        try:
            await _hydrate_snapshot()
        except Exception as e:
            logger.warning("运营指标快照恢复失败: %s", e)
        if _is_stale():
            # 只补存储/知识数量；热门扫描留给每日 0 点任务或手动刷新
            logger.info("运营指标缺少今日数据，启动补统计（不含热门知识扫描）")
            _start_job("startup", include_hot=False)

    asyncio.create_task(_bootstrap())
    asyncio.create_task(_daily_scheduler())


# ===== 接口 =====
@router.get("/overview")
async def overview(u=Depends(require_role("super_admin", "admin", "editor", "viewer"))):
    """顶部核心指标：取流水表最新记录展示（内存快照镜像）。"""
    from kb_common.clients import dingtalk_client as dt
    await dt.sync_runtime_config()
    await _hydrate_snapshot()

    # 读接口不再自动触发全量统计：一次统计要遍历钉钉 + 逐文档扫描，长达 25 分钟，
    # 期间整机 CPU 被打满、所有接口间歇性卡 3~50 秒。
    # 数据过期时返回 stale 标记，由前端提示用户点「刷新」（或等每日 0 点定时任务）。
    stale = bool(dt.is_configured()) and _is_stale()

    sv = _snapshot.get("storage") or {}
    kc = _snapshot.get("knowledge_count") or {}
    return {
        "storage": {
            "value": sv.get("value"), "unit": "GB", "month_new": "-", "mom": "-",
            "error": sv.get("error"), "fallback": sv.get("fallback", False),
            "loading": bool(dt.is_configured() and _job["running"] and not sv.get("value")),
            "updated_at": sv.get("created_at"),
        },
        "knowledge_count": {
            "value": kc.get("value"), "unit": "份", "month_new": kc.get("month_new"), "mom": "-",
            "partial": kc.get("partial"), "failed_folders": kc.get("failed_folders"),
            "loading": bool(dt.is_configured() and _job["running"] and kc.get("value") is None),
            "updated_at": kc.get("created_at"),
        },
        "dingtalk_configured": dt.is_configured(),
        "job": {"running": _job["running"], "phase": _job["phase"]},
        "stale": stale,
    }


@router.get("/knowledge-distribution")
async def knowledge_distribution(
    top_n: int | None = None,
    u=Depends(require_role("super_admin", "admin", "editor", "viewer")),
):
    """企业知识数量分布：取最新流水中的分布数据（饼图）。"""
    from kb_common.clients import dingtalk_client as dt
    await dt.sync_runtime_config()
    await _hydrate_snapshot()
    kc = _snapshot.get("knowledge_count") or {}
    items = list(kc.get("distribution") or [])
    if top_n and top_n > 0:
        items = items[:top_n]
    return {
        "items": items,
        "error": None,
        "loading": bool(dt.is_configured() and _job["running"] and not items),
    }


@router.get("/knowledge-gap-departments")
async def knowledge_gap_departments(u=Depends(require_role("super_admin", "admin", "editor", "viewer"))):
    """复用知识缺口列表的目录行，按知识库名称汇总部门知识覆盖情况。"""
    from kb_common.models import DingtalkFolderStat, KnowledgeSource
    from app.routes.knowledge_gaps import all_dingtalk_folder_rows

    async with SessionLocal() as s:
        # 与知识缺口页默认列表共用同一数据函数，确保目录范围与数量口径逐行一致。
        rows = await all_dingtalk_folder_rows(s)
        latest = await s.scalar(
            select(func.max(DingtalkFolderStat.fetched_at))
            .join(KnowledgeSource, KnowledgeSource.external_id == DingtalkFolderStat.external_id)
            .where(KnowledgeSource.source_type == "dingtalk_workspace", KnowledgeSource.enabled.is_(True))
        )

    departments: dict[str, dict] = {}
    for row in rows:
        department = row["kb_name"]
        item = departments.setdefault(department, {
            "department": department,
            "expected_folder_count": 0,
            "missing_folder_count": 0,
            "uploaded_file_count": 0,
            "covered_folder_count": 0,
        })
        item["expected_folder_count"] += 1
        document_count = row["document_count"] or 0
        folder_count = row["folder_count"] or 0
        item["uploaded_file_count"] += document_count
        # 文件夹直属子目录数或直属文件数任一大于 0，就视为目录有内容。
        if folder_count > 0 or document_count > 0:
            item["covered_folder_count"] += 1
        else:
            item["missing_folder_count"] += 1

    items = list(departments.values())
    for item in items:
        expected = item["expected_folder_count"]
        item["coverage_percent"] = round(item["covered_folder_count"] * 100 / expected, 2) if expected else 0
    items.sort(key=lambda item: (-item["coverage_percent"], item["department"]))

    total = {
        "department": "合计",
        "expected_folder_count": sum(item["expected_folder_count"] for item in items),
        "missing_folder_count": sum(item["missing_folder_count"] for item in items),
        "uploaded_file_count": sum(item["uploaded_file_count"] for item in items),
        "covered_folder_count": sum(item["covered_folder_count"] for item in items),
    }
    total["coverage_percent"] = round(total["covered_folder_count"] * 100 / total["expected_folder_count"], 2) if total["expected_folder_count"] else 0
    return {
        "items": items,
        "total": total,
        "updated_at": latest.strftime("%Y-%m-%d %H:%M") if latest else None,
        "has_snapshot": latest is not None,
    }


@router.get("/hot-documents")
async def hot_documents(u=Depends(require_role("super_admin", "admin", "editor", "viewer"))):
    """热门知识 Top20：展示最新流水记录；扫描进行中时返回实时进度与部分排名。"""
    from kb_common.clients import dingtalk_client as dt
    await dt.sync_runtime_config()
    await _hydrate_snapshot()
    # 同 /overview：读接口不再自动触发全量统计（热门扫描要 fork 上万个 dws 子进程，
    # 实测跑 25 分钟、期间整机 CPU 打满、所有接口间歇性卡 3~50 秒）。
    stale = bool(dt.is_configured()) and _is_stale()

    hot_phase = _job["running"] and _job["phase"] == "hot"
    hd = _snapshot.get("hot_docs") or {}
    if hot_phase:
        items = _top_hot_docs()               # 扫描中的实时部分排名
        scanned, total = _hot_cache["scanned"], _hot_cache["total"]
    else:
        items = hd.get("items") or []
        scanned, total = hd.get("scanned", len(items)), hd.get("total", 0)
    return {
        "items": items,
        "scanned": scanned,
        "total": total,
        "loading": bool(hot_phase),
        "error": None,
        "stale": stale,
        "updated_at": hd.get("created_at"),
    }


@router.post("/refresh-dingtalk")
async def refresh_dingtalk(u=Depends(require_role("super_admin", "admin"))):
    """手动刷新：立即重新全量统计（存储 → 数量 → 热门），结果写流水并更新展示。"""
    from kb_common.clients import dingtalk_client as dt
    await dt.sync_runtime_config()
    if not dt.is_configured():
        return {"ok": False, "error": "钉钉未配置"}
    started = _start_job("manual")
    return {"ok": True, "storage_error": None, "loading": True, "already_running": not started}
