"""杰克百晓生智能体配置与开场白接口。"""
import io
import os
import re
import zipfile

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession

from kb_common.database import get_session
from app.deps import get_current_user, require_role
from app.services.agent.config import load_agent_config, save_agent_config, TOOL_CATALOG

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])

# DeerFlow sidecar 地址（与 deerflow_runner 保持一致）
_DEERFLOW_URL = os.getenv("DEERFLOW_SERVICE_URL", "http://127.0.0.1:2027").rstrip("/")
_DEFAULT_TOKEN = "kge-internal-dev-token"


def _internal_headers() -> dict[str, str]:
    return {"X-Internal-Token": os.getenv("KB_INTERNAL_TOKEN", _DEFAULT_TOKEN)}


async def _df_get(path: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{_DEERFLOW_URL}{path}", headers=_internal_headers())
            resp.raise_for_status()
            return resp.json()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail=f"DeerFlow 智能体服务未就绪（{e.__class__.__name__}），请确认 sidecar 已启动",
        ) from e


async def _df_post(path: str, payload: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{_DEERFLOW_URL}{path}", json=payload, headers=_internal_headers())
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok", True):
                raise HTTPException(status_code=400, detail=data.get("error", "sidecar 返回失败"))
            return data
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"sidecar 请求失败：{e.response.text[:200]}") from e
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=503,
            detail=f"DeerFlow 智能体服务未就绪（{e.__class__.__name__}），请确认 sidecar 已启动",
        ) from e


async def _df_put(path: str, payload: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.put(f"{_DEERFLOW_URL}{path}", json=payload, headers=_internal_headers())
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok", True):
                raise HTTPException(status_code=400, detail=data.get("error", "sidecar 返回失败"))
            return data
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"sidecar 请求失败：{e.response.text[:200]}") from e
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=503,
            detail=f"DeerFlow 智能体服务未就绪（{e.__class__.__name__}），请确认 sidecar 已启动",
        ) from e


@router.get("/config")
async def get_agent_config(u=Depends(get_current_user),
                           s: AsyncSession = Depends(get_session)):
    """读取智能体配置（所有登录用户可读；问答页据此初始化）。"""
    return await load_agent_config(s)


@router.put("/config")
async def update_agent_config(body: dict,
                              u=Depends(require_role("super_admin", "admin")),
                              s: AsyncSession = Depends(get_session)):
    """更新智能体配置（仅管理员）。保存后通知 sidecar 热更新，新对话即时生效。"""
    saved = await save_agent_config(s, body)
    try:
        await _df_post("/v1/reconfig", {})
    except HTTPException as exc:
        import logging
        logging.getLogger(__name__).warning("sidecar reconfig notify failed: %s", exc.detail)
    return saved


@router.get("/greeting")
async def get_greeting(u=Depends(get_current_user),
                       s: AsyncSession = Depends(get_session)):
    """问答页欢迎区：开场白 + 推荐问题 + 运行开关。"""
    cfg = await load_agent_config(s)
    return {
        "agent_name": cfg["agent_name"],
        "bot_avatar": cfg.get("bot_avatar", ""),
        "greeting_enabled": cfg["greeting_enabled"],
        "greeting": cfg["greeting"],
        "suggested_questions": cfg["suggested_questions"],
        "models": cfg["models"],
        "default_model": cfg["default_model"] or (cfg["models"][0] if cfg["models"] else ""),
        "follow_up_enabled": cfg["follow_up_enabled"],
        "planning_enabled": cfg["planning_enabled"],
        "long_memory_enabled": cfg["long_memory_enabled"],
    }


# ---------------------------------------------------------------------------
# 人格（SOUL）：DeerFlow sidecar 上可热编辑的提示词配置
# ---------------------------------------------------------------------------

@router.get("/persona")
async def get_persona(u=Depends(require_role("super_admin", "admin"))):
    """读取智能体人格（SOUL.md）当前内容与默认模板。"""
    return await _df_get("/v1/persona")


@router.put("/persona")
async def update_persona(body: dict,
                         u=Depends(require_role("super_admin", "admin"))):
    """保存自定义人格（reset=true 恢复默认），保存后新对话即时生效。"""
    return await _df_post("/v1/persona", {
        "content": body.get("content"),
        "reset": bool(body.get("reset", False)),
    })


# ---------------------------------------------------------------------------
# 工具开关：智能问答可调用工具目录 + 启用状态
# ---------------------------------------------------------------------------

@router.get("/tools")
async def list_tools(u=Depends(get_current_user),
                     s: AsyncSession = Depends(get_session)):
    """工具目录及当前启用状态（所有登录用户可读）。"""
    cfg = await load_agent_config(s)
    enabled = cfg.get("tools_enabled") or {}
    return {
        "tools": [
            {**t, "enabled": bool(enabled.get(t["key"], True))}
            for t in TOOL_CATALOG
        ]
    }


# ---------------------------------------------------------------------------
# 技能库管理（ClawHub/Agent Skills 通用结构：<技能名>/SKILL.md）
# ---------------------------------------------------------------------------

def _parse_skill_frontmatter(content: str) -> dict[str, str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    meta: dict[str, str] = {}
    if not m:
        return meta
    for line in m.group(1).split("\n"):
        line = line.strip()
        if line and ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta


@router.get("/skills")
async def list_skills(u=Depends(require_role("super_admin", "admin"))):
    """列出技能库（含启用/停用状态）。"""
    return await _df_get("/v1/skills")


@router.put("/skills")
async def upsert_skill(body: dict,
                       u=Depends(require_role("super_admin", "admin"))):
    """新建/覆盖技能（name/description/content/enabled），新对话即时生效。"""
    return await _df_put("/v1/skills", {
        "name": body.get("name") or "",
        "description": body.get("description") or "",
        "content": body.get("content") or "",
        "enabled": bool(body.get("enabled", True)),
        "slug": body.get("slug") or "",
    })


@router.delete("/skills/{slug}")
async def remove_skill(slug: str,
                       u=Depends(require_role("super_admin", "admin"))):
    """删除技能。"""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.delete(
                f"{_DEERFLOW_URL}/v1/skills/{slug}", headers=_internal_headers())
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"sidecar 请求失败：{e.response.text[:200]}") from e
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=503,
            detail=f"DeerFlow 智能体服务未就绪（{e.__class__.__name__}），请确认 sidecar 已启动",
        ) from e


@router.post("/skills/import")
async def import_skill(file: UploadFile = File(...),
                       enabled: bool = True,
                       u=Depends(require_role("super_admin", "admin"))):
    """导入技能：上传 .zip 包（内含 SKILL.md，兼容 ClawHub 结构）或单个 SKILL.md/.md 文件。"""
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "上传文件为空")

    name = ""
    filename = (file.filename or "").lower()
    skill_md = ""

    if filename.endswith(".zip"):
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as e:
            raise HTTPException(400, "无效的 zip 压缩包") from e
        # 兼容两种结构：SKILL.md 在包根目录，或在单层顶层目录内（ClawHub 常见打包方式）
        candidates = [
            n for n in zf.namelist()
            if n.endswith("SKILL.md") and not n.startswith("__MACOSX")
        ]
        if not candidates:
            # 兜底：任意 .md 文件
            candidates = [n for n in zf.namelist()
                          if n.lower().endswith(".md") and not n.startswith("__MACOSX")]
        if not candidates:
            raise HTTPException(400, "压缩包中未找到 SKILL.md（技能说明文件）")
        # 优先路径最短的（最贴近根的 SKILL.md）
        candidates.sort(key=len)
        skill_md = zf.read(candidates[0]).decode("utf-8", errors="replace")
        # 技能名：SKILL.md 所在目录名
        parts = candidates[0].strip("/").split("/")
        name = parts[-2] if len(parts) >= 2 else ""
    elif filename.endswith(".md") or filename.endswith(".markdown"):
        skill_md = raw.decode("utf-8", errors="replace")
    else:
        raise HTTPException(400, "仅支持 .zip 技能包或 .md 技能说明文件")

    meta = _parse_skill_frontmatter(skill_md)
    skill_name = (meta.get("name") or name or os.path.splitext(file.filename or "skill")[0]).strip()
    description = meta.get("description", "")

    if not skill_name:
        raise HTTPException(400, "无法确定技能名称（SKILL.md frontmatter 缺少 name）")

    # 转发给 sidecar 落盘（frontmatter 不完整时由 sidecar 自动补齐）
    return await _df_put("/v1/skills", {
        "name": skill_name,
        "description": description,
        "content": skill_md,
        "enabled": enabled,
    })
