from contextlib import asynccontextmanager

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession
from kb_common.config import get_settings

settings = get_settings()

# 新建物理连接的超时：asyncpg 默认 60s，实测数据库/容器侧抖动时会卡满 60s 再抛
# TimeoutError，把单个请求拖到 90s。压到与池等待上限一致，宁可快速失败。
_connect_args: dict = {}
if settings.database_url.startswith("postgresql+asyncpg://"):
    _connect_args["timeout"] = settings.db_pool_timeout

engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    echo=False,
    # 池容量与等待上限集中配置（默认 10+20、等待 10s）：
    # 默认 5+10/30s 在前端单页并发十余个请求时会排队 30s 再抛 TimeoutError，
    # 表现为「点任何菜单都要等几秒甚至报 500」。
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout,
    pool_recycle=settings.db_pool_recycle,
    connect_args=_connect_args,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

async def get_session() -> AsyncSession:
    async with SessionLocal() as s:
        yield s


@asynccontextmanager
async def fresh_session():
    """独立短命引擎+会话：供非主事件循环的后台线程（批处理/XXL 执行器线程）使用。

    全局 engine/SessionLocal 的 asyncpg 连接池绑定首次使用它的事件循环
    （FastAPI 主 loop）；在批处理线程 asyncio.run 产生的临时 loop 里复用会报
    "got Future attached to a different loop"。此类场景每次新建独立引擎、
    用完即弃（批处理每批仅数篇，建连开销可忽略）。
    """
    eng = create_async_engine(settings.database_url, echo=False,
                              connect_args=_connect_args)
    try:
        async with async_sessionmaker(eng, expire_on_commit=False,
                                      class_=AsyncSession)() as s:
            yield s
    finally:
        await eng.dispose()


@asynccontextmanager
async def short_session():
    """一次性短会话：进出即归还连接。

    用于「只需要读一行就走」的场景（鉴权取用户、读配置），
    避免把连接占满整个请求生命周期——那正是连接池被拖垮的主因。
    """
    async with SessionLocal() as s:
        yield s


def pool_status() -> dict:
    """连接池水位（诊断用）：checkedout=正在使用，overflow=当前已建的溢出连接。"""
    p = engine.pool
    return {
        "pool_size": p.size(),
        "checked_in": p.checkedin(),
        "checked_out": p.checkedout(),
        "overflow": p.overflow(),
        "max": p.size() + p.overflow() if hasattr(p, "overflow") else p.size(),
    }


async def warm_pool(count: int | None = None, deadline_s: float = 180.0,
                    batch: int = 5) -> int:
    """渐进预热连接池：分批同时建连接、建满后归还，返回池内常驻连接数。

    为什么要预热：新建一条 PG 连接常态只要 22~40ms，但本机 Docker VM 抖动时
    实测会拖到 6~39 秒。若不预热，这批连接会在用户第一次点击菜单时才去建，
    表现就是「点任何菜单都要等几秒甚至几十秒」。

    两个实现要点（都是踩过的坑）：
    1. 必须**同时持有**多条连接才能把池撑大——串行地「连一条关一条」永远只有 1 条，
       因为关掉就还回池里、下一次复用同一条；
    2. 不能一次性并发建满（实测 15 条并发会被 VM 抖动整体拖到超时、一条都建不成），
       所以按 batch 分批、每条 8s 上限、整批失败退避 1s 重试，直到建满或到截止时间。
    """
    import time as _time

    target = count or settings.db_pool_size
    deadline = _time.monotonic() + deadline_s
    while _time.monotonic() < deadline:
        p = engine.pool
        have = p.checkedin() + p.checkedout()
        if have >= target:
            break
        conns = []
        try:
            for _ in range(min(batch, target - have)):
                conns.append(await asyncio.wait_for(engine.connect(), timeout=8))
        except Exception:  # noqa: BLE001 — 建到一半失败也没关系，已建的照常归还
            pass
        except BaseException:
            # CancelledError（外层 wait_for 熔断）：必须回收已建连接再抛出。
            # 否则半开连接泄漏会污染连接池/事件循环（uvloop 下实测后续所有
            # connect 永久挂起，lifespan 卡死在启动阶段）。
            for c in conns:
                try:
                    await c.close()
                except Exception:  # noqa: BLE001
                    pass
            raise
        for c in conns:
            try:
                await c.close()
            except Exception:  # noqa: BLE001
                pass
        if not conns:
            await asyncio.sleep(1.0)      # 整批失败：退避后重试
    return engine.pool.checkedin()
