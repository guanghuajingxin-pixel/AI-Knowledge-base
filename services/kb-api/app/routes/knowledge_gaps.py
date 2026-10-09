"""Permission-scoped directory coverage and imported knowledge ownership."""
import asyncio
import csv
import io
import logging
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, require_role
from kb_common.database import get_session
from kb_common.models import Directory, Document, DingtalkFolderStat, KnowledgeBase, KnowledgeSource

router = APIRouter(prefix="/api/v1/governance/gaps", tags=["governance"])
logger = logging.getLogger(__name__)


def visible_kbs(user):
    q = select(KnowledgeBase).where(KnowledgeBase.kb_type == "DOCUMENT")
    if user.role not in ("admin", "super_admin"):
        q = q.where(KnowledgeBase.owner_id == user.id)
    return q


async def resolve_kb_filter(s, kb_id: str | None) -> tuple[uuid.UUID | None, KnowledgeSource | None, bool]:
    """解析知识库过滤参数，返回 (本地KB-UUID, 钉钉知识源, 是否有过滤条件)。

    下拉选项来自知识源管理（钉钉知识库 external_id），同时兼容旧的本地知识库 UUID：
    - 合法 UUID：按本地知识库 ID 过滤
    - 其它值：视为知识源注册表 external_id（钉钉知识库），命中则返回知识源对象，
      由调用方实时遍历该知识库的全部文件夹
    """
    if not kb_id:
        return None, None, False
    try:
        return uuid.UUID(kb_id), None, True
    except (ValueError, AttributeError):
        pass
    src = (await s.execute(
        select(KnowledgeSource).where(
            KnowledgeSource.source_type == "dingtalk_workspace",
            KnowledgeSource.external_id == kb_id,
            KnowledgeSource.enabled.is_(True),
        )
    )).scalar_one_or_none()
    return None, src, True


# ===== 钉钉知识库文件夹快照（持久化于 dingtalk_folder_stats 表）=====
# 钉钉 wiki 接口有全局 0.4s 限流，几万文档的知识库全量遍历需数分钟，
# 因此查询接口只读 DB 快照；「刷新」按钮触发后台任务重新遍历并覆盖写入，
# 前端轮询 /dingtalk/refresh/status 展示进度，完成后自动重新查询。

_refresh_state: dict[str, dict] = {}  # external_id -> {running, done, error}


def _direct_child_folder_counts(paths: list[str]) -> dict[str, int]:
    """按路径树统计每个文件夹的直接子文件夹数（不含目录本身与孙级）。

    同一知识库内路径唯一、不带前导斜杠，根为空串：
    子文件夹路径去掉最后一段即父路径，据此一次性累加。
    """
    counts = dict.fromkeys(paths, 0)
    for p in paths:
        parent = p.rsplit('/', 1)[0] if '/' in p else ''
        # 根行（path=''）父级算得自身，跳过，避免根把自己计为子文件夹
        if parent != p and parent in counts:
            counts[parent] += 1
    return counts


async def dingtalk_folder_rows(s: AsyncSession, source: KnowledgeSource) -> list[dict]:
    """读取钉钉知识库文件夹快照行（持久化数据，不调用钉钉接口）。"""
    stats = (await s.execute(
        select(DingtalkFolderStat).where(DingtalkFolderStat.external_id == source.external_id)
        .order_by(DingtalkFolderStat.path, DingtalkFolderStat.node_id))).scalars().all()
    child_counts = _direct_child_folder_counts([r.path for r in stats])
    # 知识库首页 = 根节点：钉钉在线文档约定 /i/nodes/{nodeId}，页面在钉钉客户端/浏览器内跳转
    root_node_id = (source.config or {}).get("root_node_id") or source.external_id
    kb_url = f"https://alidocs.dingtalk.com/i/nodes/{root_node_id}"
    return [dict(directory_id=r.node_id, kb_id=source.external_id, kb_name=source.name,
                 directory_path=r.path or "（根目录）", owner=r.owner,
                 document_count=r.document_count,
                 folder_count=child_counts.get(r.path, 0), source="dingtalk",
                 kb_url=kb_url,
                 dingtalk_url=f"https://alidocs.dingtalk.com/i/nodes/{r.node_id}")
            for r in stats]


async def all_dingtalk_folder_rows(s: AsyncSession) -> list[dict]:
    """「全部知识库」查询：合并所有已启用钉钉知识库的文件夹快照（不含本地/Dify 目录）。"""
    sources = (await s.execute(
        select(KnowledgeSource).where(
            KnowledgeSource.source_type == "dingtalk_workspace",
            KnowledgeSource.enabled.is_(True),
        ))).scalars().all()
    rows: list[dict] = []
    for src in sources:
        rows.extend(await dingtalk_folder_rows(s, src))
    # 排序：知识库 → 目录路径 → 知识Owner → 文档数量
    rows.sort(key=lambda r: (r['kb_name'], r['directory_path'], r['owner'] or '', r['document_count']))
    return rows


async def _refresh_dingtalk_folders_task(source_id: int) -> None:
    """后台任务：全量遍历钉钉知识库文件夹并覆盖写入快照表。"""
    from kb_common.clients import dingtalk_client
    from kb_common.database import SessionLocal

    async with SessionLocal() as s:
        src = await s.get(KnowledgeSource, source_id)
        if src is None:
            return
        state = _refresh_state.setdefault(src.external_id, {})
        state.update(running=True, done=0, error=None, truncated=False)
        try:
            await dingtalk_client.sync_runtime_config()
            root = (src.config or {}).get("root_node_id") or src.external_id
            folders = await dingtalk_client.walk_workspace_folders(
                root, on_progress=lambda n: state.update(done=n))
            # 触及安全上限即被截断：被丢弃的子文件夹会导致其父目录文件夹数量虚记为 0，
            # 标记到状态里让前端提示「数据可能不完整」，避免静默错误
            state["truncated"] = len(folders) >= dingtalk_client.WALK_MAX_FOLDERS
            if state["truncated"]:
                logger.warning("钉钉知识库文件夹遍历达到安全上限 %d，快照可能不完整 %s",
                               dingtalk_client.WALK_MAX_FOLDERS, src.name)
            # 按 node_id 保留已维护的知识Owner，刷新不丢手动维护/批量导入成果
            owners = dict((await s.execute(
                select(DingtalkFolderStat.node_id, DingtalkFolderStat.owner)
                .where(DingtalkFolderStat.external_id == src.external_id))).all())
            now = datetime.now()
            await s.execute(delete(DingtalkFolderStat)
                            .where(DingtalkFolderStat.external_id == src.external_id))
            s.add_all([DingtalkFolderStat(external_id=src.external_id, node_id=f["node_id"],
                                          path=f["path"], document_count=f["document_count"],
                                          owner=owners.get(f["node_id"], ""),
                                          fetched_at=now)
                       for f in folders])
            await s.commit()
            logger.info("钉钉知识库文件夹快照刷新完成 %s: %d 个文件夹", src.name, len(folders))
        except Exception as e:
            state["error"] = str(e)
            logger.warning("钉钉知识库文件夹快照刷新失败 %s: %s", src.name, e)
        finally:
            state["running"] = False


async def _get_dingtalk_source(s: AsyncSession, kb_id: str) -> KnowledgeSource:
    """按 external_id 查找已启用的钉钉知识源，找不到报 404。"""
    src = (await s.execute(
        select(KnowledgeSource).where(
            KnowledgeSource.source_type == "dingtalk_workspace",
            KnowledgeSource.external_id == kb_id,
            KnowledgeSource.enabled.is_(True),
        ))).scalar_one_or_none()
    if src is None:
        raise HTTPException(404, "钉钉知识库不存在或未启用")
    return src


def start_dingtalk_refresh(loop: asyncio.AbstractEventLoop, source: KnowledgeSource) -> bool:
    """在指定事件循环上启动后台刷新任务；已在刷新中则返回 False。

    供刷新接口与知识源登记（新增钉钉库后自动预热快照）复用。
    """
    state = _refresh_state.setdefault(source.external_id, {})
    if state.get("running"):
        return False
    state.update(running=True, done=0, error=None, truncated=False)
    loop.create_task(_refresh_dingtalk_folders_task(source.id))
    return True


@router.post('/dingtalk/refresh')
async def refresh_dingtalk_folders(kb_id: str = Query(..., description="知识源 external_id"),
                                   u=Depends(require_role('super_admin', 'admin', 'editor')),
                                   s: AsyncSession = Depends(get_session)):
    """触发后台刷新：全量遍历该钉钉知识库文件夹并写入快照表。

    立即返回，进度通过 /dingtalk/refresh/status 轮询；同一知识库同时只跑一个任务。
    """
    src = await _get_dingtalk_source(s, kb_id)
    if not start_dingtalk_refresh(asyncio.get_running_loop(), src):
        return {"ok": True, "running": True, "message": "该知识库正在刷新中"}
    return {"ok": True, "running": True}


@router.get('/dingtalk/refresh/status')
async def dingtalk_refresh_status(kb_id: str = Query(..., description="知识源 external_id"),
                                  u=Depends(get_current_user),
                                  s: AsyncSession = Depends(get_session)):
    """查询刷新进度与当前快照状态（文件夹数、最近抓取时间）。

    知识源可能刚登记还未被 get_current_user 之外的流程处理，因此未命中时不报 404，
    返回空状态让前端继续轮询。
    """
    src = (await s.execute(
        select(KnowledgeSource).where(
            KnowledgeSource.source_type == "dingtalk_workspace",
            KnowledgeSource.external_id == kb_id,
        ))).scalar_one_or_none()
    state = _refresh_state.get(kb_id, {})
    folder_count, fetched_at = (await s.execute(
        select(func.count(), func.max(DingtalkFolderStat.fetched_at))
        .where(DingtalkFolderStat.external_id == kb_id))).one()
    name = src.name if src else ""
    return {"running": bool(state.get("running")), "done": int(state.get("done") or 0),
            "error": state.get("error"), "truncated": bool(state.get("truncated")),
            "folder_count": folder_count,
            "fetched_at": fetched_at.isoformat(timespec="seconds") if fetched_at else None,
            "source_name": name}


async def inventory(s, user):
    kbs = (await s.execute(visible_kbs(user))).scalars().all()
    names = {kb.id: kb.name for kb in kbs}
    dirs = (await s.execute(select(Directory).where(Directory.kb_id.in_(names))
                           .order_by(Directory.kb_id, Directory.sort_order, Directory.id))).scalars().all()
    counts = dict((await s.execute(select(Document.directory_id, func.count(Document.id))
        .where(Document.kb_id.in_(names), Document.is_deleted.is_(False))
        .group_by(Document.directory_id))).all())
    by_id = {d.id: d for d in dirs}
    # 文件夹数量：该目录的直接子目录数（不含目录本身与孙级，按 parent_id 累加）
    child_counts = dict.fromkeys((d.id for d in dirs), 0)
    for d in dirs:
        if d.parent_id in child_counts:
            child_counts[d.parent_id] += 1
    items = []
    for d in dirs:
        parts, current, seen = [], d, set()
        while current and current.id not in seen:
            seen.add(current.id)
            parts.append(current.name)
            current = by_id.get(current.parent_id)
        items.append(dict(directory_id=str(d.id), kb_id=str(d.kb_id), kb_name=names[d.kb_id],
                          directory_path="/".join(reversed(parts)), owner=d.knowledge_owner,
                          document_count=counts.get(d.id, 0),
                          folder_count=child_counts.get(d.id, 0), source="local"))
    # 排序：知识库 → 目录路径 → 知识Owner → 文档数量
    items.sort(key=lambda r: (r['kb_name'], r['directory_path'], r['owner'] or '', r['document_count']))
    return items, dirs, kbs


# 数量区间筛选：下拉预设值 → [最小值, 最大值]（None 表示无上限；空串/未知值视为不过滤）
_COUNT_RANGES = {'0': (0, 0), '1-9': (1, 9), '10-99': (10, 99), '100+': (100, None)}


def _in_count_range(value: int, rng: str | None) -> bool:
    lo, hi = _COUNT_RANGES.get(rng or '', (0, None))
    return value >= lo and (hi is None or value <= hi)


def filtered(items, has_filter, kb_uuid, owner, document_state,
             document_count: str | None = None, folder_count: str | None = None,
             path: str | None = None):
    # 目录路径模糊搜索：忽略大小写的包含匹配
    kw = (path or '').strip().lower()
    return [r for r in items
            if (not has_filter or r['kb_id'] == str(kb_uuid))
            and (not owner or r['owner'] == owner)
            and (document_state == 'all' or (r['document_count'] > 0) == (document_state == 'has'))
            and _in_count_range(r['document_count'], document_count)
            and _in_count_range(r['folder_count'], folder_count)
            and (not kw or kw in r['directory_path'].lower())]


def csv_response(rows, filename):
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    for row in rows:
        writer.writerow([("'" + str(v)) if str(v).lstrip().startswith(('=', '+', '-', '@')) else v for v in row])
    return Response(out.getvalue().encode('utf-8-sig'), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@router.get('')
async def list_gaps(kb_id: str | None = None, owner: str | None = None,
                    document_state: Literal['all', 'empty', 'has'] = 'all',
                    document_count: str | None = None, folder_count: str | None = None,
                    path: str | None = None,
                    page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
                    u=Depends(get_current_user), s: AsyncSession = Depends(get_session)):
    kb_uuid, source, has_filter = await resolve_kb_filter(s, kb_id)
    if source is not None:
        # 钉钉知识库：读取持久化快照（刷新按钮触发后台更新）
        items = await dingtalk_folder_rows(s, source)
        has_filter = False  # 行已限定在该知识库内，仅需继续应用 Owner/文档状态过滤
    elif not kb_id:
        # 「全部知识库」：只返回所有已启用钉钉知识库的目录快照
        items = await all_dingtalk_folder_rows(s)
    else:
        # 本地知识库 UUID（兼容旧链接）：读本地目录
        items, _, _ = await inventory(s, u)
    matches = filtered(items, has_filter, kb_uuid, owner, document_state, document_count, folder_count, path)
    kbs = (await s.execute(visible_kbs(u))).scalars().all()
    return dict(items=matches[(page-1)*size:page*size], total=len(matches),
                knowledge_bases=[dict(id=str(k.id), name=k.name) for k in kbs],
                owners=sorted({r['owner'] for r in items if r['owner']}))


@router.get('/export')
async def export_gaps(kb_id: str | None = None, owner: str | None = None,
                      document_state: Literal['all', 'empty', 'has'] = 'all',
                      document_count: str | None = None, folder_count: str | None = None,
                      path: str | None = None,
                      u=Depends(get_current_user), s: AsyncSession = Depends(get_session)):
    kb_uuid, source, has_filter = await resolve_kb_filter(s, kb_id)
    if source is not None:
        items = await dingtalk_folder_rows(s, source)
        has_filter = False
    elif not kb_id:
        items = await all_dingtalk_folder_rows(s)
    else:
        items, _, _ = await inventory(s, u)
    return csv_response([['目录ID', '知识库', '目录路径', '知识Owner', '文档数量', '文件夹数量']] +
        [[r['directory_id'], r['kb_name'], r['directory_path'], r['owner'], r['document_count'],
          r.get('folder_count', 0)]
         for r in filtered(items, has_filter, kb_uuid, owner, document_state,
                           document_count, folder_count, path)], 'knowledge-gaps.csv')


@router.get('/owner-template')
async def owner_template(kb_id: str | None = None, u=Depends(get_current_user),
                         s: AsyncSession = Depends(get_session)):
    """下载知识Owner导入模板。

    选中钉钉知识库时导出该库全部文件夹行（含当前 Owner），编辑后可直接导回；
    未选时导出本地知识库目录（兼容旧行为）。
    """
    _, source, _ = await resolve_kb_filter(s, kb_id)
    if source is not None:
        rows = await dingtalk_folder_rows(s, source)
    else:
        rows, _, _ = await inventory(s, u)
    return csv_response([['目录ID', '知识库', '目录路径', '知识Owner']] +
        [[r['directory_id'], r['kb_name'], r['directory_path'], r['owner']] for r in rows], 'knowledge-owners.csv')


class OwnerIn(BaseModel):
    owner: str = Field(max_length=200)


class DingtalkOwnerIn(BaseModel):
    kb_id: str = Field(description="知识源 external_id")
    node_id: str = Field(description="钉钉文件夹节点 ID")
    owner: str = Field(default='', max_length=200)


@router.put('/dingtalk/owner')
async def update_dingtalk_owner(body: DingtalkOwnerIn,
                                u=Depends(require_role('super_admin', 'admin', 'editor')),
                                s: AsyncSession = Depends(get_session)):
    """维护钉钉知识库文件夹的知识Owner（留空清除）。"""
    src = await _get_dingtalk_source(s, body.kb_id)
    stat = (await s.execute(select(DingtalkFolderStat).where(
        DingtalkFolderStat.external_id == src.external_id,
        DingtalkFolderStat.node_id == body.node_id))).scalar_one_or_none()
    if not stat:
        raise HTTPException(404, '文件夹不在快照中，请先刷新数据')
    stat.owner = body.owner.strip()
    await s.commit()
    return {'ok': True}


class DingtalkNotifyIn(BaseModel):
    kb_id: str = Field(description="知识源 external_id")
    node_id: str = Field(description="钉钉文件夹节点 ID")


@router.post('/dingtalk/notify')
async def notify_dingtalk_owner(body: DingtalkNotifyIn,
                                u=Depends(require_role('super_admin', 'admin', 'editor')),
                                s: AsyncSession = Depends(get_session)):
    """通过钉钉企业机器人向知识Owner发送单聊通知：该目录下的知识为空。

    前置校验：文件夹在快照中、已维护 Owner、无文档；Owner 按姓名在
    通讯录精确匹配 userid。需在系统配置填写 robotCode，且应用开通
    「企业内机器人发送消息权限」「通讯录个人信息读权限」。
    """
    from kb_common.clients import dingtalk_client

    src = await _get_dingtalk_source(s, body.kb_id)
    stat = (await s.execute(select(DingtalkFolderStat).where(
        DingtalkFolderStat.external_id == src.external_id,
        DingtalkFolderStat.node_id == body.node_id))).scalar_one_or_none()
    if not stat:
        raise HTTPException(404, '文件夹不在快照中，请先刷新数据')
    if not (stat.owner or '').strip():
        raise HTTPException(400, '该文件夹尚未维护知识Owner，请先维护后再通知')
    if (stat.document_count or 0) > 0:
        raise HTTPException(400, '该目录已有文档，无需发送补充通知')
    path = stat.path or '（根目录）'

    await dingtalk_client.sync_runtime_config()
    if not dingtalk_client._robot_code().strip():
        raise HTTPException(400, '尚未配置钉钉机器人 robotCode，请先在【系统配置 → 钉钉设置】中填写')
    try:
        candidates = await dingtalk_client.search_users_by_name(stat.owner.strip())
    except Exception as e:
        raise HTTPException(502, f'钉钉通讯录搜索失败：{e}')
    exact = next((c for c in candidates if c['name'] == stat.owner.strip()), None)
    if exact is None:
        raise HTTPException(400,
            f'钉钉通讯录中没有与「{stat.owner}」精确匹配的员工，请核对 Owner 是否为员工姓名'
            f'（当前候选 {len(candidates)} 人）')

    content = f'【{src.name} / {path}】该目录下的知识为空，请尽快补充，谢谢！'
    try:
        await dingtalk_client.send_text_message([exact['userid']], content)
    except Exception as e:
        raise HTTPException(502, f'钉钉发送消息失败：{e}')
    logger.info("知识缺口通知已发送 %s / %s -> %s(%s)", src.name, path, stat.owner, exact['userid'])
    return {'ok': True, 'message': f'已通过钉钉私聊通知 {stat.owner}'}


@router.put('/{directory_id}/owner')
async def update_owner(directory_id: uuid.UUID, body: OwnerIn,
                       u=Depends(require_role('super_admin', 'admin', 'editor')),
                       s: AsyncSession = Depends(get_session)):
    d = (await s.execute(select(Directory).where(Directory.id == directory_id,
         Directory.kb_id.in_(visible_kbs(u).with_only_columns(KnowledgeBase.id))))).scalar_one_or_none()
    if not d:
        raise HTTPException(404, '目录不存在或无权限')
    d.knowledge_owner = body.owner.strip()
    await s.commit()
    return {'ok': True}


@router.post('/owners/import')
async def import_owners(file: UploadFile, u=Depends(require_role('super_admin', 'admin', 'editor')),
                        s: AsyncSession = Depends(get_session)):
    """批量导入知识Owner，本地目录与钉钉知识库文件夹均支持。

    匹配优先级：目录ID（本地UUID或钉钉节点ID）→ 知识库＋目录路径
    （钉钉路径与快照一致、不带前导斜杠；根目录填「（根目录）」）。
    """
    content = await file.read(5 * 1024 * 1024 + 1)
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(400, '文件不能超过 5MB')
    try:
        if (file.filename or '').lower().endswith('.xlsx'):
            from openpyxl import load_workbook
            book = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            try:
                values = list(book.active.iter_rows(values_only=True))
            finally:
                book.close()
        elif (file.filename or '').lower().endswith('.csv'):
            values = list(csv.reader(io.StringIO(content.decode('utf-8-sig'))))
        else:
            raise ValueError('仅支持 UTF-8 CSV 或 XLSX 文件')
        headers = [str(v or '').strip() for v in values[0]]
        if '知识Owner' not in headers or not ('目录ID' in headers or {'知识库', '目录路径'} <= set(headers)):
            raise ValueError('需要知识Owner，以及目录ID或知识库＋目录路径列')
        if len(values) > 10001:
            raise ValueError('单次最多导入 10000 行')
    except Exception as exc:
        raise HTTPException(400, f'无法读取导入文件：{exc}')

    items, dirs, _ = await inventory(s, u)
    local_by_id = {str(d.id): d for d in dirs}
    local_by_path = {(r['kb_name'], r['directory_path']): r['directory_id'] for r in items}
    # 钉钉文件夹：所有钉钉知识源的快照行，按节点ID与（知识库名, 路径）建索引
    dt_pairs = (await s.execute(
        select(DingtalkFolderStat, KnowledgeSource.name)
        .join(KnowledgeSource, KnowledgeSource.external_id == DingtalkFolderStat.external_id)
        .where(KnowledgeSource.source_type == "dingtalk_workspace"))).all()
    dt_by_node = {stat.node_id: stat for stat, _ in dt_pairs}
    dt_by_path = {(name, stat.path): stat for stat, name in dt_pairs}

    changes, errors, seen = [], [], set()  # changes: (kind, target_obj, owner)
    for line, cells in enumerate(values[1:], 2):
        if not any(v is not None and str(v).strip() for v in cells):
            continue
        row = dict(zip(headers, [str(v).strip() if v is not None else '' for v in cells]))
        key, kb, path = row.get('目录ID', ''), row.get('知识库', ''), row.get('目录路径', '')
        owner = row.get('知识Owner', '')
        target = None  # (kind, 对象)
        if key and key in local_by_id:
            target = ('local', local_by_id[key])
        elif key and key in dt_by_node:
            target = ('dingtalk', dt_by_node[key])
        elif kb and path:
            dt_path = '' if path == '（根目录）' else path
            if (kb, path) in local_by_path:
                target = ('local', local_by_id[local_by_path[(kb, path)]])
            elif (kb, dt_path) in dt_by_path:
                target = ('dingtalk', dt_by_path[(kb, dt_path)])
        if target is None:
            errors.append(f'第 {line} 行：目录不存在或匹配不唯一（目录ID={key or "空"}）')
        elif not owner or len(owner) > 200:
            errors.append(f'第 {line} 行：Owner 为空或超过 200 字')
        else:
            kind, obj = target
            dedupe = ('local', str(obj.id)) if kind == 'local' else ('dingtalk', obj.id)
            if dedupe in seen:
                errors.append(f'第 {line} 行：目录重复出现')
            else:
                seen.add(dedupe)
                changes.append((kind, obj, owner))
    if errors:
        raise HTTPException(400, '导入未保存。' + '；'.join(errors[:20]))
    if not changes:
        raise HTTPException(400, '没有可导入的数据')
    for kind, obj, owner in changes:
        if kind == 'local':
            obj.knowledge_owner = owner
        else:
            obj.owner = owner
    await s.commit()
    return {'updated': len(changes)}
