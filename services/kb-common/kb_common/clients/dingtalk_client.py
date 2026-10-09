"""钉钉开放平台客户端：企业知识库（Wiki）、通讯录、AI 表格（多维表）。

服务运营看板 / 知识中心 / 治理标准三个模块：

1. 知识库（Wiki）
   - accessToken：POST https://api.dingtalk.com/v1.0/oauth2/accessToken
   - 知识库列表：GET  https://api.dingtalk.com/v2.0/wiki/workspaces
   - 节点列表：  GET  https://api.dingtalk.com/v2.0/wiki/nodes
   - 企业存储总量：GET https://api.dingtalk.com/v1.0/storage/orgs/{corpId}
     （需「企业存储企业读权限 Storage.Org.Read」，与钉钉后台存储口径一致）
   - get_all_knowledge_files() 深度并发遍历所有团队知识库，返回文件平铺列表。

2. 通讯录
   - userid → 姓名：POST https://oapi.dingtalk.com/topapi/v2/user/get（带缓存）。

3. AI 表格（多维表，notable）
   - 列出记录：POST https://api.dingtalk.com/v1.0/notable/bases/{baseId}/sheets/{sheetId}/records/list
     （需「AI 表格应用读权限」）。

配置读取顺序：显式注入 / DB settings 表（sync_runtime_config）> .env（get_settings）。
操作人需具备目标知识库读权限，operatorId 为该用户的 unionId。
"""
import asyncio
import logging
import random
import time
from typing import Any

import httpx
from kb_common.config import get_settings

logger = logging.getLogger(__name__)

API_BASE = "https://api.dingtalk.com"
OAPI_BASE = "https://oapi.dingtalk.com"

# 并发与限流：钉钉标准版有调用频次限制，过高并发会触发 403/overlimit。
# 全量遍历信号量（限制同时在飞的 wiki/nodes 请求数）
_WALK_CONCURRENCY = 3
# 单次请求失败（限流/超时）时的退避重试
_MAX_RETRY = 5
_RETRY_BASE_DELAY = 1.0
# 全局请求节流：wiki/nodes 限流严格（实测串行 2 QPS 稳定、3 并发即批量 403），
# 所有 wiki 请求之间至少间隔该秒数（令牌桶的最小发包间隔）。
_RL_MIN_INTERVAL = 0.4
_rl_lock = asyncio.Lock()
_rl_next_at = 0.0


async def _throttle() -> None:
    """全局最小请求间隔节流（跨所有协程），保证 wiki 类接口 QPS 不超限。"""
    global _rl_next_at
    async with _rl_lock:
        now = time.monotonic()
        wait = _rl_next_at - now
        if wait > 0:
            await asyncio.sleep(wait)
        _rl_next_at = time.monotonic() + _RL_MIN_INTERVAL

# =====《杰克知识管理规范》AI 多维表坐标 =====
# baseId / sheetId 来自该多维表 URL；如需更换规范表，可通过 .env 的
# DINGTALK_STANDARDS_BASE_ID / DINGTALK_STANDARDS_SHEET_ID 覆盖。
# 这些是平台默认的《杰克知识管理规范》多维表坐标；不能留空，否则治理
# 标准页在每次刷新时都会在请求钉钉之前失败。
STANDARDS_BASE_ID = "N7dx2rn0JbNoBXLeuNw2ONvPJMGjLRb3"
STANDARDS_SHEET_ID = "29Wa4h3"

# 运行时注入的配置（来自 DB settings 表，优先级高于 .env）
_injected: dict[str, str] = {}
_token_cache: dict[str, Any] = {"token": "", "expire_at": 0.0}
_corp_id_cache: dict[str, Any] = {"corp_id": "", "expire_at": 0.0}
_name_cache: dict[str, str] = {}
# 最近一次全量遍历的请求统计（requests=成功的节点请求数，failed_folders=重试耗尽仍失败的目录数）
_walk_info: dict[str, int] = {"requests": 0, "failed_folders": 0}
# 运行时配置（DB settings 表）读取缓存：TTL 内复用，避免每个请求都为读配置占一条 DB 连接
_RUNTIME_CFG_TTL = 30.0
_runtime_cfg_cache: dict[str, float] = {"loaded_at": 0.0}
# 知识库列表缓存：变动极少但被高频读取（页面挂载 + 后台遍历），TTL 内直接复用。
# 按 operator 分键：不同登录用户可见的知识库范围不同，共用一份缓存会串权限。
_WS_TTL = 60.0
_ws_cache: dict[str, dict[str, Any]] = {}
_ws_lock = asyncio.Lock()
# 后台刷新单飞标记：缓存过期时只允许一个刷新任务在飞（按 operator 分键）
_ws_refresh: dict[str, bool] = {}


# ===================== 配置 =====================
def configure(app_key: str = "", app_secret: str = "", operator_union_id: str = "",
              robot_code: str = "") -> None:
    """注入钉钉配置（来自 DB settings 表，覆盖 .env）。空值不覆盖。"""
    incoming = {
        "dingtalk_app_key": app_key,
        "dingtalk_app_secret": app_secret,
        "dingtalk_operator_union_id": operator_union_id,
        "dingtalk_robot_code": robot_code,
    }
    # 凭证/操作人真的变了才清缓存：换人后知识库可见范围不同，旧 token 也不再适用
    identity_keys = ("dingtalk_app_key", "dingtalk_app_secret", "dingtalk_operator_union_id")
    changed = any(incoming[k] and _injected.get(k) != incoming[k] for k in identity_keys)
    for k, v in incoming.items():
        if v:
            _injected[k] = v
    if changed:
        _token_cache["token"] = ""
        _token_cache["expire_at"] = 0.0
        _corp_id_cache["corp_id"] = ""
        _corp_id_cache["expire_at"] = 0.0
        invalidate_workspaces_cache()


async def sync_runtime_config() -> None:
    """从 DB settings 表读取钉钉配置并即时生效（供知识中心/治理标准调用）。

    带 30s TTL 缓存：运营看板/治理标准等接口每次请求都会调用本函数，
    若每次都开一条新会话跑 SQL，会在连接池里额外占坑（曾是把池抽干的帮凶之一）。
    「系统配置」页保存凭证后调用 invalidate_runtime_config() 立即生效，不受 TTL 影响。
    """
    global STANDARDS_BASE_ID, STANDARDS_SHEET_ID
    now = time.monotonic()
    if _runtime_cfg_cache["loaded_at"] and now - _runtime_cfg_cache["loaded_at"] < _RUNTIME_CFG_TTL:
        return

    from sqlalchemy import select
    from kb_common.database import short_session
    from kb_common.models import Setting

    keys = ["dingtalk_app_key", "dingtalk_app_secret", "dingtalk_operator_union_id",
            "dingtalk_robot_code"]
    vals: dict[str, str] = {k: "" for k in keys}
    async with short_session() as s:
        rows = (await s.execute(select(Setting).where(Setting.key.in_(keys)))).scalars().all()
    for row in rows:
        if row.value:
            vals[row.key] = str(row.value)
    configure(vals["dingtalk_app_key"], vals["dingtalk_app_secret"],
              vals["dingtalk_operator_union_id"], vals["dingtalk_robot_code"])
    _runtime_cfg_cache["loaded_at"] = now

    import os
    # 空环境变量不应覆盖内置默认表；这样 .env.example 中保留的空配置也安全。
    STANDARDS_BASE_ID = os.getenv("DINGTALK_STANDARDS_BASE_ID", "").strip() or STANDARDS_BASE_ID
    STANDARDS_SHEET_ID = os.getenv("DINGTALK_STANDARDS_SHEET_ID", "").strip() or STANDARDS_SHEET_ID


def invalidate_runtime_config() -> None:
    """清空运行时配置缓存：系统配置页保存钉钉凭证后调用，保证下一次读取即拿到新值。"""
    _runtime_cfg_cache["loaded_at"] = 0.0


def _cfg(key: str) -> str:
    return _injected.get(key) or getattr(get_settings(), key, "") or ""


def _app_key() -> str:
    return _cfg("dingtalk_app_key")


def _app_secret() -> str:
    return _cfg("dingtalk_app_secret")


def _operator_id() -> str:
    return _cfg("dingtalk_operator_union_id")


def _robot_code() -> str:
    return _cfg("dingtalk_robot_code")


def is_configured() -> bool:
    return bool(_app_key() and _app_secret() and _operator_id())


# ===================== accessToken =====================
async def _get_access_token() -> str:
    now = time.time()
    if _token_cache["token"] and _token_cache["expire_at"] > now + 300:
        return _token_cache["token"]
    key, secret = _app_key(), _app_secret()
    if not key or not secret:
        raise RuntimeError("钉钉 AppKey/AppSecret 未配置")
    url = f"{API_BASE}/v1.0/oauth2/accessToken"
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(url, json={"appKey": key, "appSecret": secret})
        resp.raise_for_status()
        data = resp.json()
    token = data.get("accessToken") or data.get("access_token")
    if not token:
        raise RuntimeError(f"获取钉钉 accessToken 失败：{data}")
    _token_cache["token"] = token
    _token_cache["expire_at"] = now + data.get("expireIn", 7200)
    return token


def _headers(token: str) -> dict[str, str]:
    return {"x-acs-dingtalk-access-token": token, "Content-Type": "application/json"}


def _raise_readable(resp: httpx.Response) -> None:
    """以可读信息抛错：优先透出钉钉 JSON body 的 code/message/所需权限点，
    避免上层只拿到 httpx 的 "Client error '403 Forbidden' for url ..."。"""
    try:
        data = resp.json()
        msg = data.get("message") or data.get("errmsg") or ""
        code = data.get("code") or ""
        scopes = (data.get("accessdenieddetail") or {}).get("requiredScopes") or []
        if msg:
            suffix = f"（需开通权限：{', '.join(scopes)}）" if scopes else ""
            raise RuntimeError(f"钉钉接口拒绝（{resp.status_code} {code}）：{msg}{suffix}")
    except RuntimeError:
        raise
    except Exception:
        pass
    resp.raise_for_status()


async def _request_with_retry(method: str, url: str, *, client: httpx.AsyncClient,
                              max_retry: int | None = None,
                              **kwargs) -> httpx.Response:
    """带限流退避重试的 HTTP 请求。

    对限流（403/429）、服务端错误（5xx）、超时做指数退避重试；
    对明确的权限不足（permissionDenied/no.priviledge，body 可辨）不重试。

    max_retry：重试次数预算。默认 5 次适合后台全量遍历（可以慢慢等）；
    页面挂载时同步等待的轻量接口必须传小值（如 2）——5 次指数退避
    (1+2+4+8+16s) 叠加每次最长 60s 超时，单次调用最坏能耗掉几分钟，
    实测把「知识治理 / 钉钉知识同步」页卡住 50 秒。
    """
    attempts = max_retry if max_retry and max_retry > 0 else _MAX_RETRY
    last_exc: Exception | None = None
    resp: httpx.Response | None = None
    for attempt in range(attempts):
        try:
            resp = await client.request(method, url, **kwargs)
            # 限流 / 服务端错误 → 退避重试
            if resp.status_code in (403, 429) or resp.status_code >= 500:
                body_low = (resp.text or "").lower()
                # 明确权限不足不重试（body 含 permissionDenied / requiredScopes / 尚未开通 等）
                perm_markers = ("permissiondenied", "requiredscopes", "no.priviledge",
                                "尚未开通", "accessdenieddetail")
                if resp.status_code == 403 and any(m in body_low for m in perm_markers):
                    _raise_readable(resp)
                delay = _RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.5)
                logger.warning("钉钉请求被限流/错误 %s，%.1fs 后重试(%d/%d): %s",
                               resp.status_code, delay, attempt + 1, attempts, url)
                await asyncio.sleep(delay)
                continue
            return resp
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_exc = e
            delay = _RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.5)
            await asyncio.sleep(delay)
    if last_exc:
        raise last_exc
    # 重试用尽，返回最后一次响应交由上层处理
    return resp  # type: ignore[return-value]


# ===================== 知识库 =====================
async def _list_workspaces_remote(timeout: float = 60.0,
                                  max_retry: int | None = None,
                                  operator_union_id: str | None = None) -> list[dict[str, Any]]:
    """真正去钉钉拉知识库列表（自动分页，受全局节流约束）。

    timeout / max_retry：后台刷新用默认宽松值；用户正在等的冷启动路径传小值，
    避免钉钉抖动时把页面卡住几十秒。
    operator_union_id：按登录用户维度取可见范围；不传回退全局服务账号。
    """
    operator = operator_union_id or _operator_id()
    if not operator:
        raise RuntimeError("钉钉操作人 UnionId 未配置")
    token = await _get_access_token()
    url = f"{API_BASE}/v2.0/wiki/workspaces"
    params = {"operatorId": operator, "maxResults": 30}
    out: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        while True:
            await _throttle()
            resp = await _request_with_retry("GET", url, client=client, max_retry=max_retry,
                                             params=params, headers=_headers(token))
            _raise_readable(resp)
            data = resp.json()
            out.extend(data.get("workspaces") or [])
            nxt = data.get("nextToken")
            if not nxt:
                break
            params["nextToken"] = nxt
    return out


def _schedule_ws_refresh(operator: str) -> None:
    """后台单飞刷新知识库列表缓存：失败就保留旧数据，下次访问再试。"""
    if _ws_refresh.get(operator):
        return
    _ws_refresh[operator] = True

    async def _run() -> None:
        try:
            items = await _list_workspaces_remote(operator_union_id=operator)
            entry = _ws_cache.setdefault(operator, {"at": 0.0, "items": None})
            entry["at"] = time.monotonic()
            entry["items"] = items
        except Exception as e:  # noqa: BLE001 — 刷新失败不影响已缓存数据的展示
            logger.warning("后台刷新钉钉知识库列表失败，沿用旧缓存: %s", e)
        finally:
            _ws_refresh[operator] = False

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:      # 不在事件循环里（同步调用方）——放弃本次刷新
        _ws_refresh[operator] = False


async def list_workspaces(use_cache: bool = True,
                          operator_union_id: str | None = None) -> list[dict[str, Any]]:
    """获取操作人可见的知识库列表（60s 缓存 + 过期后台刷新 + 冷启动单飞）。

    知识库列表变动极少，但「钉钉知识同步」页每次挂载、后台全量遍历都会读它。
    缓存过期时**先返回旧数据、再后台刷新**：钉钉限流/抖动（实测单次调用可达 50s）
    不会再变成用户界面上的等待。只有进程内完全没有缓存时才同步等一次，
    且用 15s 超时 + 2 次重试的短预算兜底。
    需要强制取最新时传 use_cache=False，或在钉钉配置变更后调 invalidate_workspaces_cache()。
    operator_union_id：按登录用户维度取可见范围（缓存按 operator 分键）；不传回退全局服务账号。
    """
    operator = operator_union_id or _operator_id()
    if not use_cache:
        return await _list_workspaces_remote(operator_union_id=operator)

    entry = _ws_cache.get(operator)
    if entry and entry["items"] is not None:
        if time.monotonic() - entry["at"] >= _WS_TTL:
            _schedule_ws_refresh(operator)      # 过期：旧值先顶上，后台悄悄刷新
        return entry["items"]

    # 冷启动没有任何缓存：只能同步等一次（短超时 + 小重试预算），并发请求共用同一次拉取
    async with _ws_lock:
        entry = _ws_cache.get(operator)
        if entry and entry["items"] is not None:
            return entry["items"]
        fetched = await _list_workspaces_remote(timeout=15.0, max_retry=2,
                                                operator_union_id=operator)
        _ws_cache[operator] = {"at": time.monotonic(), "items": fetched}
        return fetched


def invalidate_workspaces_cache() -> None:
    """清空知识库列表缓存（钉钉操作人/凭证变更后调用）。"""
    _ws_cache.clear()


async def list_nodes(parent_node_id: str,
                     operator_union_id: str | None = None) -> list[dict[str, Any]]:
    """获取某父节点下的直接子节点，自动分页。operator_union_id 不传回退全局服务账号。"""
    operator = operator_union_id or _operator_id()
    if not operator:
        raise RuntimeError("钉钉操作人 UnionId 未配置")
    token = await _get_access_token()
    url = f"{API_BASE}/v2.0/wiki/nodes"
    params = {"parentNodeId": parent_node_id, "operatorId": operator, "maxResults": 50}
    out: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        while True:
            await _throttle()
            resp = await _request_with_retry("GET", url, client=client,
                                             params=params, headers=_headers(token))
            _raise_readable(resp)
            data = resp.json()
            out.extend(data.get("nodes") or [])
            nxt = data.get("nextToken")
            if not nxt:
                break
            params["nextToken"] = nxt
    return out


# 全量遍历文件夹数安全上限：防递归失控（如权限异常导致死循环）。
# 历史默认 5000 曾把「营销军团」等大知识库静默截断——超过上限的文件夹
# 不落快照，其父目录文件夹数量被错记为 0；真实企业库可达数千~数万级，放宽到 50000。
WALK_MAX_FOLDERS = 50000


async def walk_workspace_folders(root_node_id: str, max_folders: int = WALK_MAX_FOLDERS,
                                 on_progress=None,
                                 operator_union_id: str | None = None) -> list[dict[str, Any]]:
    """递归遍历钉钉知识库，返回全部文件夹及各文件夹直属文档数量。

    直属文档 = 文件夹直接子节点中的非文件夹节点，包含在线文档（钉钉在线编辑）
    和本地上传到知识库的文件两类。返回行字段：
    - node_id: 文件夹节点 ID（根节点也作为一行返回，path 为 ""）
    - path: 从根开始的文件夹路径（不带前导斜杠），如 "规章制度/研发流程"；根为 ""
    - document_count: 该文件夹直属文档数（不含子文件夹内的文档）
    max_folders 为安全上限（达到后停止递归，调用方应据 len(folders) >= max_folders
    判断是否截断并向用户提示数据可能不完整）；on_progress(done: int) 在每完成一个
    文件夹后回调（用于进度展示）。
    operator_union_id：按登录用户维度遍历（仅能看到其有权限的节点）；不传回退全局。
    """
    folders: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(5)

    async def walk(node_id: str, path: str) -> None:
        if len(folders) >= max_folders:
            return
        async with sem:
            try:
                nodes = await list_nodes(node_id, operator_union_id=operator_union_id)
            except Exception as e:
                logger.warning("钉钉获取节点失败 %s: %s（该文件夹文档数记为 0）", path or "/", e)
                nodes = []
        doc_count = 0
        child_folders: list[tuple[str, str]] = []
        for n in nodes:
            if n.get("type") == "FOLDER":
                fname = n.get("name") or ""
                # .dlink 为「文件夹快捷方式」：其子节点无法通过 API 列出（递归只会
                # 得到空），作为父文件夹的一个文档计数；不递归，避免产生 0 文档的
                # 假目录行且父文件夹漏计
                if (n.get("extension") or "") == "dlink" or fname.endswith(".dlink"):
                    doc_count += 1
                else:
                    child_folders.append((n.get("nodeId") or "", fname))
            else:
                doc_count += 1
        folders.append({"node_id": node_id, "path": path, "document_count": doc_count})
        if on_progress:
            try:
                on_progress(len(folders))
            except Exception:
                pass
        # 并发遍历子文件夹（信号量限流），否则大知识库顺序递归过慢
        if child_folders:
            await asyncio.gather(*[
                walk(fid, f"{path}/{fname}" if path else fname)
                for fid, fname in child_folders if fid
            ])

    await walk(root_node_id, "")
    return folders


async def download_document(node_id: str,
                            operator_union_id: str | None = None) -> tuple[bytes, str]:
    """按 wiki 节点 ID 下载文件原始内容，返回 (内容 bytes, 文件名)。

    钉钉 wiki 节点的 url 字段是在线预览页（alidocs.dingtalk.com），直接 HTTP 下载
    会拿到 HTML；必须走 queryDentryId → downloadInfos/query → OSS 直链才能取到原文件。
    operator_union_id 不传回退全局服务账号。
    """
    operator = operator_union_id or _operator_id()
    if not operator:
        raise RuntimeError("钉钉操作人 UnionId 未配置")
    token = await _get_access_token()
    headers = _headers(token)

    async with httpx.AsyncClient(timeout=120.0) as client:
        # 1. wiki 节点 → dentry（spaceId + dentryId）
        await _throttle()
        d1 = await _request_with_retry(
            "GET", f"{API_BASE}/v2.0/doc/dentries/{node_id}/queryDentryId",
            client=client, params={"operatorId": operator}, headers=headers,
        )
        d1.raise_for_status()
        d1_data = d1.json()
        space_id = d1_data.get("spaceId")
        dentry_id = d1_data.get("dentryId")
        if not space_id or not dentry_id:
            raise RuntimeError(f"queryDentryId 缺少 spaceId/dentryId: {d1_data}")

        # 2. 换取 OSS 下载地址与签名头
        await _throttle()
        d2 = await _request_with_retry(
            "POST",
            f"{API_BASE}/v1.0/storage/spaces/{space_id}/dentries/{dentry_id}/downloadInfos/query",
            client=client, params={"unionId": operator},
            json={"option": {"preferIntranet": False}}, headers=headers,
        )
        d2.raise_for_status()
        d2_data = d2.json()
        header_info = d2_data.get("headerSignatureInfo") or {}
        urls = header_info.get("resourceUrls") or []
        if not urls:
            raise RuntimeError(f"downloadInfos/query 未返回下载地址: {d2_data}")
        dl_headers = dict(header_info.get("headers") or {})

        # 3. 从 OSS 下载文件内容
        resp = await client.get(urls[0], headers=dl_headers, timeout=120.0)
        from .source_integrity import downloaded_file
        return downloaded_file(resp, urls[0])


async def _walk_workspace_files(workspace: dict, sem: asyncio.Semaphore,
                                operator_union_id: str | None = None) -> list[dict]:
    """并发深度遍历一个知识库，返回文件平铺列表（含目录路径、创建人等）。"""
    ws_name = workspace.get("name", "")
    ws_id = workspace.get("workspaceId")
    root_id = workspace.get("rootNodeId")
    files: list[dict] = []
    if not root_id:
        return files

    async def walk(node_id: str, dir_path: str) -> None:
        async with sem:
            try:
                nodes = await list_nodes(node_id, operator_union_id=operator_union_id)
                _walk_info["requests"] += 1
            except Exception as e:
                _walk_info["failed_folders"] += 1
                logger.warning("钉钉获取节点失败 %s/%s: %s（该目录下文件将缺失）", ws_name, node_id, e)
                nodes = []
        child_folders = []
        for n in nodes:
            name = n.get("name", "")
            if n.get("type") == "FOLDER":
                if n.get("hasChildren"):
                    child_folders.append((n.get("nodeId"), f"{dir_path}/{name}"))
            else:
                files.append({
                    "node_id": n.get("nodeId"),
                    "workspace_id": n.get("workspaceId") or ws_id,
                    "workspace_name": ws_name,
                    "name": name,
                    "directory_path": dir_path or "/",
                    "category": n.get("category"),
                    "extension": n.get("extension"),
                    "size": int(n.get("size") or 0),
                    "url": n.get("url"),
                    "creator_id": n.get("creatorId") or "",
                    "created_at": n.get("createTime"),
                    "modified_at": n.get("modifiedTime"),
                })
        if child_folders:
            await asyncio.gather(*[walk(fid, fpath) for fid, fpath in child_folders])

    await walk(root_id, "")
    return files


async def get_all_knowledge_files(operator_union_id: str | None = None) -> list[dict]:
    """并发遍历全部团队知识库，返回所有文件的平铺列表。

    operator_union_id：按登录用户维度遍历（仅其有权限的库/节点）；不传回退全局服务账号。
    """
    workspaces = await list_workspaces(operator_union_id=operator_union_id)
    teams = [w for w in workspaces if w.get("type") != "PERSONAL"]
    _walk_info["requests"] = 0
    _walk_info["failed_folders"] = 0
    sem = asyncio.Semaphore(_WALK_CONCURRENCY)
    results = await asyncio.gather(
        *[_walk_workspace_files(w, sem, operator_union_id=operator_union_id) for w in teams],
        return_exceptions=True,
    )
    out: list[dict] = []
    for r in results:
        if isinstance(r, Exception):
            logger.warning("遍历知识库失败: %s", r)
        else:
            out.extend(r)
    logger.info("钉钉知识库遍历完成：%d 个库、%d 个文件、节点请求 %d 次、失败目录 %d 个",
                len(teams), len(out), _walk_info["requests"], _walk_info["failed_folders"])
    return out


def get_last_walk_info() -> dict[str, int]:
    """最近一次全量遍历的请求统计（供运营看板判断数据是否完整）。"""
    return dict(_walk_info)


# ===================== 企业存储 / 运营统计 =====================
async def get_corp_id(operator_union_id: str | None = None) -> str:
    now = time.time()
    if _corp_id_cache["corp_id"] and _corp_id_cache["expire_at"] > now:
        return _corp_id_cache["corp_id"]
    wss = await list_workspaces(operator_union_id=operator_union_id)
    corp_id = next((w.get("corpId") for w in wss if w.get("corpId")), "")
    if not corp_id:
        raise RuntimeError("未能从知识库列表中获取企业 corpId")
    _corp_id_cache["corp_id"] = corp_id
    _corp_id_cache["expire_at"] = now + 3600
    return corp_id


async def _get_org_storage_used_api() -> int:
    """企业存储 API 直连（需应用开通 Storage.Org.Read），权限不足抛 RuntimeError。"""
    corp_id = await get_corp_id()
    token = await _get_access_token()
    url = f"{API_BASE}/v1.0/storage/orgs/{corp_id}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await _request_with_retry(
                "GET", url, client=client,
                params={"unionId": _operator_id()}, headers=_headers(token))
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                raise RuntimeError(
                    "缺少「企业存储企业读权限(Storage.Org.Read)」，请在钉钉开放平台为应用开通该权限后重试"
                ) from e
            raise
        data = resp.json()
    used = 0
    for p in (data.get("org") or {}).get("partitions") or []:
        used += int((p.get("quota") or {}).get("used") or 0)
    return used


# ===== 钉钉官方 CLI（dws）兜底：用户 OAuth 授权线，不依赖应用权限 =====
_CLI_QUOTA_TTL = 600
_cli_quota_cache: dict[str, Any] = {"used": 0, "apps": 0, "expire_at": 0.0}


def _dws_bin() -> str:
    """钉钉 CLI 路径：优先环境变量 DWS_BIN，否则项目根 tools/dws/dws。"""
    import os
    from pathlib import Path
    custom = os.getenv("DWS_BIN")
    if custom:
        return custom
    # dingtalk_client.py → clients → kb_common → kb-common → services → 项目根
    return str(Path(__file__).resolve().parents[4] / "tools" / "dws" / "dws")


async def get_org_storage_used_via_cli() -> int:
    """通过钉钉官方 CLI（dws drive quota apps）读取企业存储用量汇总（字节）。

    走 dws auth login 扫码的用户 OAuth 授权，与企业的 appKey/AppSecret 权限线
    相互独立，可在应用未开通 Storage.Org.Read 时获取与钉钉后台一致的总量
    （钉盘/在线文档/会议/AI 听记等全应用用量之和）。结果缓存 10 分钟。
    CLI 未安装/未扫码/调用失败时抛 RuntimeError。
    """
    import json as _json
    from pathlib import Path
    now = time.time()
    if _cli_quota_cache["expire_at"] > now:
        return _cli_quota_cache["used"]
    dws = _dws_bin()
    if not Path(dws).is_file():
        raise RuntimeError(f"钉钉 CLI 不存在：{dws}（先完成 dws auth login 扫码授权）")
    total = 0
    app_count = 0
    cursor: str | None = None
    for _ in range(20):  # 分页保护，最多 20 页
        args = [dws, "drive", "quota", "apps", "-f", "json", "--limit", "50"]
        if cursor:
            args += ["--cursor", cursor]
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("dws drive quota apps 调用超时")
        if proc.returncode != 0:
            raise RuntimeError(
                f"dws 调用失败(exit={proc.returncode})：{(err or b'').decode(errors='ignore')[:200]}")
        try:
            data = _json.loads(out.decode() or "{}")
        except _json.JSONDecodeError as e:
            raise RuntimeError(f"dws 返回非 JSON：{out.decode(errors='ignore')[:200]}") from e
        for app in data.get("apps") or []:
            total += int(app.get("used") or 0)
            app_count += 1
        cursor = data.get("nextToken")
        if not cursor:
            break
    _cli_quota_cache["used"] = total
    _cli_quota_cache["apps"] = app_count
    _cli_quota_cache["expire_at"] = now + _CLI_QUOTA_TTL
    logger.info("钉钉 CLI 企业存储用量：%d 字节（%d 个应用）", total, app_count)
    return total


async def get_org_storage_used() -> int:
    """企业存储已用容量（字节），与钉钉知识后台口径一致。

    优先企业存储 API（需 Storage.Org.Read 权限）；权限不足或调用失败时
    自动降级为钉钉 CLI（用户扫码授权，dws drive quota apps 汇总）。
    """
    try:
        return await _get_org_storage_used_api()
    except RuntimeError as api_err:
        try:
            return await get_org_storage_used_via_cli()
        except Exception as cli_err:
            raise RuntimeError(f"{api_err}；CLI 兜底也失败：{cli_err}") from cli_err


async def get_node_stats_via_cli(node_id: str) -> dict[str, int]:
    """通过钉钉 CLI（dws drive stats）读取单个节点的阅读/访问/编辑/评论统计。

    返回 {read_count, visit_count, edit_count, comment_count}；调用失败抛 RuntimeError。
    """
    import json as _json
    dws = _dws_bin()
    proc = await asyncio.create_subprocess_exec(
        dws, "drive", "stats", "--node", str(node_id), "-f", "json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("dws drive stats 调用超时")
    if proc.returncode != 0:
        raise RuntimeError((err or b"").decode(errors="ignore")[:200] or f"dws exit={proc.returncode}")
    try:
        data = _json.loads(out.decode() or "{}")
    except _json.JSONDecodeError as e:
        raise RuntimeError("dws 返回非 JSON") from e
    if data.get("success") is False or data.get("error"):
        raise RuntimeError(str(data.get("error") or data.get("message") or data)[:200])
    return {
        "read_count": int(data.get("readCount") or 0),
        "visit_count": int(data.get("visitCount") or 0),
        "edit_count": int(data.get("editCount") or 0),
        "comment_count": int(data.get("commentCount") or 0),
    }


async def get_all_workspaces_stats() -> list[dict]:
    """按知识库聚合统计（文件数 / 总大小 / 文件明细），供运营看板使用。"""
    files = await get_all_knowledge_files()
    stats: dict[str, dict] = {}
    for f in files:
        wid = f.get("workspace_id")
        bucket = stats.setdefault(wid, {
            "workspace_id": wid,
            "name": f.get("workspace_name", "未命名"),
            "file_count": 0,
            "folder_count": 0,
            "total_size": 0,
            "files": [],
        })
        bucket["file_count"] += 1
        bucket["total_size"] += f.get("size", 0)
        bucket["files"].append({
            "name": f.get("name", ""),
            "size": f.get("size", 0),
            "node_id": f.get("node_id"),
            "url": f.get("url"),
            "category": f.get("category"),
            "extension": f.get("extension"),
            "modified_time": f.get("modified_at"),
        })
    return list(stats.values())


# ===================== 通讯录 =====================
async def _fetch_user_name(userid: str, token: str) -> str:
    url = f"{OAPI_BASE}/topapi/v2/user/get?access_token={token}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, json={"userid": userid})
        data = resp.json()
    if data.get("errcode") == 0:
        return (data.get("result") or {}).get("name") or ""
    return ""


async def get_user_name_map(user_ids: list[str]) -> dict[str, str]:
    """批量解析 userid → 姓名（带缓存，并发拉取缺失项）。"""
    missing = [u for u in dict.fromkeys(user_ids) if u and u not in _name_cache]
    if missing:
        token = await _get_access_token()
        sem = asyncio.Semaphore(_WALK_CONCURRENCY)

        async def _one(uid: str) -> None:
            async with sem:
                try:
                    _name_cache[uid] = await _fetch_user_name(uid, token) or uid
                except Exception:
                    _name_cache[uid] = uid

        await asyncio.gather(*[_one(u) for u in missing])
    return {u: _name_cache.get(u, u) for u in user_ids if u}


async def search_users_by_name(query: str) -> list[dict]:
    """按姓名关键词搜索企业通讯录，返回 [{userid, name}]（按 topapi 反查姓名）。

    用于「知识 Owner 姓名 → 可收通知的 userid」解析；需应用开通
    「通讯录个人信息读权限」。候选为空时返回 []，由调用方决定错误提示。
    """
    token = await _get_access_token()
    url = f"{API_BASE}/v1.0/contact/users/search"
    async with httpx.AsyncClient(timeout=20.0) as client:
        await _throttle()
        resp = await _request_with_retry("POST", url, client=client, headers=_headers(token),
                                         json={"queryWord": query, "offset": 0, "limit": 20})
        if resp.status_code != 200:
            raise RuntimeError(f"钉钉通讯录搜索失败({resp.status_code})：{(resp.text or '')[:200]}")
        data = resp.json() or {}
    # 官方字段 userIdList；兼容 result 包装的历史变体
    ids = (data.get("userIdList") or data.get("useridList")
           or (data.get("result") or {}).get("userIdList") or [])
    ids = [i for i in ids if i]
    if not ids:
        return []
    name_map = await get_user_name_map(ids)
    return [{"userid": i, "name": name_map.get(i, "")} for i in ids]


# ===================== 企业内机器人消息 =====================
async def send_text_message(user_ids: list[str], content: str) -> dict:
    """企业内部机器人向员工发送单聊文本消息（msgKey=sampleText）。

    需在钉钉开放平台为应用开通「企业内机器人发送消息权限」，并在系统配置中
    填写 robotCode（dingtalk_robot_code）。返回钉钉响应（含 processQueryKey）。
    """
    import json as _json
    robot = _robot_code()
    if not robot:
        raise RuntimeError("钉钉 robotCode 未配置（系统配置 → 钉钉设置）")
    if not user_ids:
        raise RuntimeError("收件人为空，无法发送")
    token = await _get_access_token()
    url = f"{API_BASE}/v1.0/robot/oToMessages/send"
    body = {"robotCode": robot, "userIds": user_ids, "msgKey": "sampleText",
            "msgParam": _json.dumps({"content": content}, ensure_ascii=False)}
    async with httpx.AsyncClient(timeout=20.0) as client:
        await _throttle()
        resp = await _request_with_retry("POST", url, client=client, headers=_headers(token), json=body)
        if resp.status_code != 200:
            # 钉钉新版错误 body：{"code": "...", "message": "无权限访问robot(不属于当前应用)"}
            try:
                msg = (resp.json() or {}).get("message") or (resp.text or "")
            except Exception:
                msg = resp.text or ""
            raise RuntimeError(f"钉钉机器人发送失败({resp.status_code})：{(msg or '')[:200]}")
        try:
            return resp.json() or {}
        except Exception:
            return {}


# ===================== 免登（H5 微应用） =====================
async def get_user_info_by_code(code: str) -> dict:
    """免登：authCode → 员工信息 {userid, name, unionid}。

    POST https://oapi.dingtalk.com/topapi/v2/user/getuserinfo
    需应用开通「通讯录个人信息读权限」。失败抛 RuntimeError（调用方转 401）。
    """
    token = await _get_access_token()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(f"{OAPI_BASE}/topapi/v2/user/getuserinfo",
                                 params={"access_token": token}, json={"code": code})
        data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"钉钉免登失败(errcode={data.get('errcode')})：{data.get('errmsg')}")
    r = data.get("result") or {}
    userid = (r.get("userid") or "").strip()
    if not userid:
        raise RuntimeError(f"钉钉免登未返回 userid：{data}")
    return {"userid": userid, "name": (r.get("name") or "").strip(),
            "unionid": (r.get("unionid") or "").strip()}


async def get_user_info_by_qr_code(code: str) -> dict:
    """PC 扫码登录（统一登录码）：code → 用户级 token → unionId → 企业 userid。

    1. POST /v1.0/oauth2/userAccessToken（clientId/clientSecret + 授权码）
    2. GET  /v1.0/contact/users/me（用户级 token）→ unionId / nick
    3. POST topapi/user/getbyunionid（企业 token）→ 企业内 userid（绑定表主键用）
    失败抛 RuntimeError（调用方转 401）。
    """
    key, secret = _app_key(), _app_secret()
    if not key or not secret:
        raise RuntimeError("钉钉 AppKey/AppSecret 未配置")
    async with httpx.AsyncClient(timeout=20.0) as client:
        r1 = await client.post(f"{API_BASE}/v1.0/oauth2/userAccessToken",
                               json={"clientId": key, "clientSecret": secret,
                                     "code": code, "grantType": "authorization_code"})
        if r1.status_code != 200:
            raise RuntimeError(f"钉钉扫码登录换 token 失败({r1.status_code})：{(r1.text or '')[:200]}")
        user_token = (r1.json() or {}).get("accessToken") or ""
        if not user_token:
            raise RuntimeError(f"钉钉扫码登录未返回用户 accessToken：{(r1.text or '')[:200]}")
        r2 = await client.get(f"{API_BASE}/v1.0/contact/users/me", headers=_headers(user_token))
        if r2.status_code != 200:
            raise RuntimeError(f"钉钉读取扫码用户信息失败({r2.status_code})：{(r2.text or '')[:200]}")
        me = r2.json() or {}
    unionid = (me.get("unionId") or "").strip()
    name = (me.get("nick") or "").strip()
    if not unionid:
        raise RuntimeError(f"钉钉扫码登录未返回 unionId：{me}")
    token = await _get_access_token()
    async with httpx.AsyncClient(timeout=15.0) as client:
        r3 = await client.post(f"{OAPI_BASE}/topapi/user/getbyunionid",
                               params={"access_token": token}, json={"unionid": unionid})
        data = r3.json() or {}
    userid = ""
    if data.get("errcode") == 0:
        userid = ((data.get("result") or {}).get("userid") or "").strip()
    if not userid:
        raise RuntimeError(f"unionId 反查企业 userid 失败(errcode={data.get('errcode')})：{data.get('errmsg')}")
    return {"userid": userid, "name": name, "unionid": unionid}


# ===================== 机器人回复（问答场景） =====================
async def reply_session_webhook(webhook: str, title: str, text: str) -> None:
    """通过消息回调自带的 sessionWebhook 回复 markdown（单聊/群 @ 通用）。"""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(webhook, json={
            "msgtype": "markdown",
            "markdown": {"title": title, "text": text},
        })
        resp.raise_for_status()


async def send_markdown_message(user_ids: list[str], title: str, text: str) -> dict:
    """企业内部机器人单聊 markdown（msgKey=sampleMarkdown），sessionWebhook 失效时的兜底通道。"""
    import json as _json
    robot = _robot_code()
    if not robot:
        raise RuntimeError("钉钉 robotCode 未配置（系统配置 → 钉钉设置）")
    if not user_ids:
        raise RuntimeError("收件人为空，无法发送")
    token = await _get_access_token()
    url = f"{API_BASE}/v1.0/robot/oToMessages/send"
    body = {"robotCode": robot, "userIds": user_ids, "msgKey": "sampleMarkdown",
            "msgParam": _json.dumps({"title": title, "text": text}, ensure_ascii=False)}
    async with httpx.AsyncClient(timeout=20.0) as client:
        await _throttle()
        resp = await _request_with_retry("POST", url, client=client, headers=_headers(token), json=body)
        if resp.status_code != 200:
            raise RuntimeError(f"钉钉机器人单聊发送失败({resp.status_code})：{(resp.text or '')[:200]}")
        try:
            return resp.json() or {}
        except Exception:
            return {}


async def send_group_markdown(open_conversation_id: str, title: str, text: str) -> dict:
    """企业内部机器人向群会话发 markdown（robot/groupMessages/send），群聊兜底通道。"""
    import json as _json
    robot = _robot_code()
    if not robot:
        raise RuntimeError("钉钉 robotCode 未配置（系统配置 → 钉钉设置）")
    if not open_conversation_id:
        raise RuntimeError("群会话 ID 为空，无法发送")
    token = await _get_access_token()
    url = f"{API_BASE}/v1.0/robot/groupMessages/send"
    body = {"robotCode": robot, "openConversationId": open_conversation_id,
            "msgKey": "sampleMarkdown",
            "msgParam": _json.dumps({"title": title, "text": text}, ensure_ascii=False)}
    async with httpx.AsyncClient(timeout=20.0) as client:
        await _throttle()
        resp = await _request_with_retry("POST", url, client=client, headers=_headers(token), json=body)
        if resp.status_code != 200:
            raise RuntimeError(f"钉钉机器人群聊发送失败({resp.status_code})：{(resp.text or '')[:200]}")
        try:
            return resp.json() or {}
        except Exception:
            return {}


async def send_action_card(user_ids: list[str], title: str, text: str,
                           buttons: list[dict[str, str]], btn_orientation: str = "1") -> dict:
    """企业内部机器人单聊动作卡片（msgKey=sampleActionCard），带按钮跳转。

    buttons: [{"title": "通过", "actionURL": "https://..."}, ...]
    btn_orientation: "0"=竖向排列，"1"=横向排列。
    点击按钮跳转到 actionURL（审批页面），由页面内完成审批操作。
    """
    import json as _json
    robot = _robot_code()
    if not robot:
        raise RuntimeError("钉钉 robotCode 未配置（系统配置 → 钉钉设置）")
    if not user_ids:
        raise RuntimeError("收件人为空，无法发送")
    token = await _get_access_token()
    url = f"{API_BASE}/v1.0/robot/oToMessages/send"
    msg_param = {
        "title": title,
        "text": text,
        "btnOrientation": btn_orientation,
        "btns": buttons,
    }
    body = {"robotCode": robot, "userIds": user_ids, "msgKey": "sampleActionCard",
            "msgParam": _json.dumps(msg_param, ensure_ascii=False)}
    async with httpx.AsyncClient(timeout=20.0) as client:
        await _throttle()
        resp = await _request_with_retry("POST", url, client=client, headers=_headers(token), json=body)
        if resp.status_code != 200:
            raise RuntimeError(f"钉钉动作卡片发送失败({resp.status_code})：{(resp.text or '')[:200]}")
        try:
            return resp.json() or {}
        except Exception:
            return {}


# ===================== AI 表格（多维表） =====================
async def list_aitable_records(base_id: str, sheet_id: str, timeout: float = 20.0,
                               max_retry: int = 2,
                               operator_union_id: str | None = None) -> list[dict]:
    """读取 AI 表格（多维表）指定数据表的全部记录，自动分页。

    返回 [{id, fields: {字段名: 值, ...}}, ...]。需「AI 表格应用读权限」。

    timeout/max_retry 默认取「交互接口」的短预算（20s × 2 次）：目前唯一调用方是
    治理标准页的同步等待接口，若沿用后台遍历的 60s × 5 次退避，
    一次钉钉限流就会变成界面上几十秒的白屏。
    operator_union_id 不传回退全局服务账号。
    """
    if not base_id or not sheet_id:
        raise RuntimeError(
            "规范表 baseId/sheetId 未配置（请设置 DINGTALK_STANDARDS_BASE_ID / "
            "DINGTALK_STANDARDS_SHEET_ID）"
        )
    operator = operator_union_id or _operator_id()
    token = await _get_access_token()
    url = f"{API_BASE}/v1.0/notable/bases/{base_id}/sheets/{sheet_id}/records/list"
    params = {"operatorId": operator}
    body: dict[str, Any] = {"maxResults": 100}
    records: list[dict] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        while True:
            resp = await _request_with_retry("POST", url, client=client, max_retry=max_retry,
                                             params=params, json=body, headers=_headers(token))
            resp.raise_for_status()
            data = resp.json()
            records.extend(data.get("records") or [])
            if not data.get("hasMore"):
                break
            body["nextToken"] = data.get("nextToken")
    return records
