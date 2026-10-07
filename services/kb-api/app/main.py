from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routes import auth, users, internal, knowledge_base, directory, document, search, settings_route, knowledge_center, knowledge_library, dify_route, ragflow_route, operate, governance, agent_route, agent_internal, chat_session_route, qa_route, sync_route, process_route, knowledge_gaps, metrics_route, sensitive, masking, mineru_route, managed_library


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时从数据库 settings 表加载配置到运行时环境，重启/刷新后始终以 DB 为准。"""
    import os
    from sqlalchemy import select
    from kb_common.database import SessionLocal
    from kb_common.models import Setting

    # DB 设置键 → 环境变量名
    env_map = {
        "llm_base_url": "LLM_BASE_URL",
        "llm_api_key": "LLM_API_KEY",
        "llm_model": "LLM_MODEL",
        "mineru_api_key": "MINERU_API_KEY",
        "dify_base_url": "DIFY_BASE_URL",
        "dify_api_key": "DIFY_API_KEY",
        "ragflow_base_url": "RAGFLOW_BASE_URL",
        "ragflow_api_key": "RAGFLOW_API_KEY",
        "dingtalk_app_key": "DINGTALK_APP_KEY",
        "dingtalk_app_secret": "DINGTALK_APP_SECRET",
        "dingtalk_operator_union_id": "DINGTALK_OPERATOR_UNION_ID",
    }
    try:
        async with SessionLocal() as s:
            rows = (await s.execute(select(Setting))).scalars().all()
        for row in rows:
            if row.value:
                env_key = env_map.get(row.key)
                if env_key:
                    os.environ[env_key] = str(row.value)
    except Exception:
        pass  # 表不存在/数据库未就绪时静默降级到 .env 默认值

    # 连接池预热（两段式）：启动阶段最多阻塞 8s 建连接，剩下的交给后台补齐。
    # 新建一条 PG 连接常态 22~40ms，但本机 Docker VM 抖动时实测要 6~39s；
    # 纯后台预热来不及——重启后的第一个请求实测被拖到 34s。
    # 尽量把这笔开销放在「还没开始接请求」的启动阶段，用户第一次点击就不必替我们买单。
    try:
        import asyncio
        import logging
        from kb_common.database import warm_pool
        _perf_log = logging.getLogger(__name__)

        try:
            warmed = await asyncio.wait_for(warm_pool(batch=3), timeout=8)
            _perf_log.info("启动阶段连接池预热：%s 条常驻连接", warmed)
        except Exception as exc:  # noqa: BLE001 — 预热超时/失败都不该阻塞启动
            _perf_log.warning("启动阶段连接池预热未完成，转后台继续: %r", exc)
            # 熔断后同步重置连接池：被取消的 connect 可能留下半开连接，
            # 不清掉会污染后续取连接（uvloop 下实测后续 connect 永久挂起、启动卡死）。
            try:
                from kb_common.database import engine as _kb_engine
                _kb_engine.sync_engine.dispose()
            except Exception:  # noqa: BLE001
                pass

        async def _warm_pool_bg() -> None:
            try:
                warmed = await warm_pool()
                _perf_log.info("数据库连接池预热完成：%s 条常驻连接", warmed)
            except Exception as exc:  # noqa: BLE001
                _perf_log.warning("连接池预热中断（不影响服务）: %r", exc)

        asyncio.create_task(_warm_pool_bg())
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("连接池预热任务启动失败（不影响启动）: %r", e)

    # 旧版单值配置 → LLM/Dify profiles 表一次性迁移（模型配置页下线后存量配置仍可见可管理）
    try:
        from app.routes.settings_route import seed_profiles_from_legacy_settings
        async with SessionLocal() as s:
            await seed_profiles_from_legacy_settings(s)
    except Exception:
        pass  # 迁移失败不阻塞启动；系统配置页读取列表时会再次尝试

    # 运营看板：每日 0 点定时统计（缺今日数据则启动即补统计）
    from app.routes import operate
    operate.start_operate_scheduler()

    # 钉钉知识库 → Dify 定时增量同步调度器（按各同步源 cron 字段注册任务）
    try:
        from app.services.sync.scheduler import start_sync_scheduler
        start_sync_scheduler()
    except Exception as e:
        # 同步调度器启动失败不应影响主服务（如依赖缺失/表未建）
        import logging
        logging.getLogger(__name__).warning("同步调度器启动失败: %s", e)

    # 钉钉机器人 Stream 长连接（开关/凭证齐全才启动，fail fast 不假跑）
    try:
        from app.services import dingtalk_bot
        await dingtalk_bot.refresh_bot()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("钉钉机器人启动检查失败: %s", e)

    try:
        yield
    finally:
        # 关闭应用（含开发热重载）时停止后台线程，避免遗留调度任务。
        try:
            from app.services.sync.scheduler import shutdown_sync_scheduler
            shutdown_sync_scheduler()
        except Exception:
            pass
        try:
            from app.services import dingtalk_bot
            await dingtalk_bot.stop_bot()
        except Exception:
            pass


app = FastAPI(title="KB API", lifespan=lifespan)

# CORS 白名单：默认覆盖 vite dev / 生产 nginx / 内网 IP，也可用 KGE_CORS_ORIGINS 逗号分隔覆盖。
# 说明：nginx 同源部署（前端 8080 → /api/v1 → kb-api 8001）浏览器不发跨域预检，
# 白名单主要给「本地起前端 + 远程 kb-api」「其它内网服务跨域调 API」用。
import os as _os_cors
_cors_env = _os_cors.environ.get("KGE_CORS_ORIGINS", "").strip()
if _cors_env:
    _cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
else:
    _cors_origins = [
        "http://localhost:5173",   # vite dev
        "http://localhost:8080",   # 本地生产端口
        "http://localhost:3000",   # 常见前端调试端口
        "http://127.0.0.1:5173",
        "http://127.0.0.1:8080",
        "http://10.10.166.2:8080", # 内网 AI-KB 生产
        "http://10.10.166.2",
    ]
app.add_middleware(CORSMiddleware, allow_origins=_cors_origins,
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


# 慢请求日志：把「哪个接口把界面拖住了」直接打在日志里，默认阈值 1s。
import logging as _logging
import os as _os
import time as _time

_perf_logger = _logging.getLogger("kbapi.perf")
_SLOW_REQUEST_MS = float(_os.getenv("KGE_SLOW_REQUEST_MS", "1000"))


@app.middleware("http")
async def log_slow_requests(request, call_next):
    t0 = _time.perf_counter()
    response = await call_next(request)
    ms = (_time.perf_counter() - t0) * 1000
    if ms >= _SLOW_REQUEST_MS:
        _perf_logger.warning("SLOW %s %s %.0fms status=%s",
                             request.method, request.url.path, ms, response.status_code)
    return response


app.include_router(auth.router)
app.include_router(users.router)
app.include_router(internal.router)
app.include_router(knowledge_base.router)
app.include_router(directory.router)
app.include_router(document.router)
app.include_router(search.router)
from app.routes import open_api
app.include_router(open_api.router)
from app.routes import platform_keys
app.include_router(platform_keys.router)
app.include_router(settings_route.router)
app.include_router(knowledge_center.router)
app.include_router(knowledge_library.router)
app.include_router(managed_library.router)
app.include_router(managed_library.image_router)
from app.routes import material_library
app.include_router(material_library.router)
app.include_router(material_library.components_router)
app.include_router(sensitive.router)
app.include_router(masking.router)
app.include_router(dify_route.router)
app.include_router(ragflow_route.router)
app.include_router(operate.router)
app.include_router(governance.router)
app.include_router(knowledge_gaps.router)
app.include_router(agent_route.router)
app.include_router(agent_internal.router)
app.include_router(chat_session_route.router)
app.include_router(qa_route.router)
app.include_router(sync_route.router)
app.include_router(process_route.router)
app.include_router(metrics_route.router)
app.include_router(mineru_route.router)

@app.get("/health")
def health(): return {"status": "ok"}


@app.get("/health/pool")
def health_pool():
    """连接池水位诊断：排查「点任何菜单都卡几秒」时先看这里。

    checked_out 长期贴近 pool_size+overflow，说明有请求/后台任务长时间占着连接。
    """
    from kb_common.database import engine as async_engine, settings as _s
    from app.services.sync.sync_database import sync_engine

    def _stat(p) -> dict:
        return {"pool_size": p.size(), "checked_in": p.checkedin(),
                "checked_out": p.checkedout(), "overflow": p.overflow()}

    return {
        "async": _stat(async_engine.pool),
        "sync": _stat(sync_engine.pool),
        "config": {"pool_size": _s.db_pool_size, "max_overflow": _s.db_max_overflow,
                   "pool_timeout": _s.db_pool_timeout},
    }
