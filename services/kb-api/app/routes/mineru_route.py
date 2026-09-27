"""MinerU 解析引擎 · 统一契约 API（本地 mineru-kit / 云端 SaaS 双引擎）。

调用方只认统一契约（schema 见 mineru_contract.py，FastAPI /docs 可查）：
  - GET  /v1/health                 健康检查 + capabilities（引擎能力发现）
  - GET  /v1/tiers                  语义档位 speed/balanced/quality
  - POST /v1/uploads                创建上传会话
  - PUT  /v1/uploads/{id}/content   上传原始字节（octet-stream）
  - POST /v1/uploads/{id}/complete  完成上传 → file_id
  - POST /v1/parse/jobs             创建解析任务（file_id 源，可多文件）
  - GET  /v1/parse/jobs             任务列表
  - GET  /v1/parse/jobs/{id}        轮询任务（status / progress / output_files）
  - DELETE /v1/parse/jobs/{id}      取消任务（云端 SaaS 返回 409）
  - GET  /v1/files/{id}/content     下载产物（markdown / structured_json）

引擎选择由 X-Mineru-Base 头决定（本地为 Base URL，云端为 "cloud"），对调用方仅
体现为 capabilities 差异。本地引擎透传 V1 形状并做两处归一化：产物 middle_json →
structured_json、档位 → 语义三级；云端由 mineru_saas 适配器翻译为 V4 契约。
显式路由未覆盖的本地路径（kit 私有端点）由末尾 catch-all 兜底透传，云端一律 404。

仅面向管理员的测试台能力，需 JWT（require_role super_admin/admin）。
"""
from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app.deps import require_role
from app.routes import mineru_saas
from app.routes.mineru_contract import (
    SEMANTIC_TIERS,
    JobCreateIn,
    JobListOut,
    JobOut,
    OkOut,
    TiersOut,
    UploadCompleteOut,
    UploadCreateIn,
    UploadOut,
    resolve_tier,
)

router = APIRouter(prefix="/api/v1/mineru", tags=["mineru"])

# 默认引擎：云端 SaaS。
# 生产环境（10.10.166.2 等）本地 mineru-kit 通常未部署，若继续默认 127.0.0.1:8010
# 会让所有未显式带 X-Mineru-Base 的请求 502。改为默认 cloud 后：
#   - 未带头 → 走 SaaS
#   - 显式带 base URL → 走本地 kit（保留开发/内网自建能力）
#   - 显式带 "cloud" → 走 SaaS
DEFAULT_BASE = "cloud"
# 上传大文件 / 解析结果下载给足余量；建任务本身是异步的、秒级返回
FORWARD_TIMEOUT = httpx.Timeout(300.0, connect=10.0)

# 不透传的请求头（hop-by-hop / 会干扰转发的头）
HOP_HEADERS = {"host", "content-length", "connection", "authorization", "x-mineru-base", "accept-encoding"}


def _engine(request: Request) -> tuple[str, str]:
    """('cloud', 'cloud') 或 ('local', base_url)。

    - 头缺失 / 空串 / "cloud"（大小写不敏感）→ 云端 SaaS
    - 其余 → 本地 kit（base_url 已 strip 尾斜杠）
    """
    raw = (request.headers.get("x-mineru-base") or "").strip()
    if not raw or raw.lower() == "cloud":
        return ("cloud", "cloud")
    return ("local", raw.rstrip("/"))


def _local_capabilities() -> dict:
    return {
        "engine": "local",
        "engine_label": "MinerU Kit Local",
        "tiers": [t["id"] for t in SEMANTIC_TIERS],
        "output_formats": ["markdown", "structured_json"],
        "cancelable": True,
        "page_range": True,
        "max_file_mb": 200,
    }


def _norm_output_files(of: Any) -> Any:
    """本地 kit 产物 middle_json → 统一契约 structured_json。"""
    if not isinstance(of, dict):
        return of
    if "structured_json" not in of and "middle_json" in of:
        of["structured_json"] = of.pop("middle_json")
    return of


def _norm_job(d: dict) -> dict:
    for f in d.get("files") or []:
        if isinstance(f, dict):
            f["output_files"] = _norm_output_files(f.get("output_files"))
    raw = d.get("tier")
    if raw:
        d["tier"], d["raw_tier"] = resolve_tier(raw, "local")
        d["raw_tier"] = raw
    return d


def _to_kit_formats(formats: list[str]) -> list[str]:
    """请求方向反向归一化：统一契约 structured_json → 本地 kit 认知的 middle_json。"""
    return [("middle_json" if f == "structured_json" else f) for f in formats or []]


async def _local_forward(request: Request, base: str, path: str, *,
                         json_body: Any = None, raw_body: bytes | None = None) -> Response:
    """同源透传到本地 kit：方法/查询串/头/体原样（HOP 头除外）。"""
    if not base.startswith(("http://", "https://")):
        raise HTTPException(400, f"MinerU 服务地址无效：{base}（需以 http:// 或 https:// 开头）")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
    content = raw_body
    if content is None and json_body is not None:
        content = json.dumps(json_body, ensure_ascii=False).encode()
    async with httpx.AsyncClient(timeout=FORWARD_TIMEOUT) as client:
        try:
            r = await client.request(request.method, f"{base}/{path}", headers=headers,
                                     params=dict(request.query_params), content=content)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"MinerU 服务连接失败（{base}）：{e.__class__.__name__}: {e}")
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"))


# ---------- 契约端点（显式类型化，供 OpenAPI /docs） ----------

@router.get("/v1/health", response_model=None, summary="健康检查 + 引擎能力发现")
async def health(request: Request, _u=Depends(require_role("super_admin", "admin"))) -> Response:
    engine, base = _engine(request)
    if engine == "cloud":
        return JSONResponse(await mineru_saas.saas_health())
    resp = await _local_forward(request, base, "v1/health")
    if resp.status_code != 200:
        return resp
    try:
        d = json.loads(resp.body)
    except ValueError:
        return resp
    formats = [(f if f != "middle_json" else "structured_json")
               for f in (d.get("features") or {}).get("output_formats") or []]
    d["features"] = {"output_formats": formats}
    d["capabilities"] = _local_capabilities()
    return JSONResponse(d)


@router.get("/v1/tiers", response_model=TiersOut, summary="语义档位（speed/balanced/quality）")
async def tiers(request: Request, _u=Depends(require_role("super_admin", "admin"))) -> Response:
    engine, _base = _engine(request)
    if engine == "cloud":
        return JSONResponse(mineru_saas.saas_tiers())
    return JSONResponse({"data": [
        {"id": t["id"], "description": t["description"], "raw_tier": t["local"]}
        for t in SEMANTIC_TIERS
    ]})


@router.post("/v1/uploads", response_model=UploadOut, summary="创建上传会话")
async def create_upload(request: Request, body: UploadCreateIn,
                        _u=Depends(require_role("super_admin", "admin"))) -> Response:
    engine, base = _engine(request)
    if engine == "cloud":
        return JSONResponse(await mineru_saas.saas_create_upload(body.model_dump()))
    return await _local_forward(request, base, "v1/uploads", json_body=body.model_dump())


@router.put("/v1/uploads/{upload_id}/content", response_model=OkOut, summary="上传原始字节")
async def put_upload_content(request: Request, upload_id: str, body: bytes = Body(...),
                             _u=Depends(require_role("super_admin", "admin"))) -> Response:
    engine, base = _engine(request)
    if engine == "cloud":
        return JSONResponse(await mineru_saas.saas_put_upload_content(upload_id, body))
    return await _local_forward(request, base, f"v1/uploads/{upload_id}/content", raw_body=body)


@router.post("/v1/uploads/{upload_id}/complete", response_model=UploadCompleteOut, summary="完成上传")
async def complete_upload(request: Request, upload_id: str,
                          _u=Depends(require_role("super_admin", "admin"))) -> Response:
    engine, base = _engine(request)
    if engine == "cloud":
        return JSONResponse(await mineru_saas.saas_complete_upload(upload_id))
    return await _local_forward(request, base, f"v1/uploads/{upload_id}/complete")


@router.post("/v1/parse/jobs", response_model=JobOut, summary="创建解析任务")
async def create_job(request: Request, body: JobCreateIn,
                     _u=Depends(require_role("super_admin", "admin"))) -> Response:
    engine, base = _engine(request)
    payload = body.model_dump(exclude_none=True)
    if engine == "cloud":
        return JSONResponse(await mineru_saas.saas_create_job(payload))
    _semantic, raw = resolve_tier(body.tier, "local")
    payload["tier"] = raw
    payload["output_formats"] = _to_kit_formats(payload.get("output_formats"))
    resp = await _local_forward(request, base, "v1/parse/jobs", json_body=payload)
    if resp.status_code == 200:
        try:
            return JSONResponse(_norm_job(json.loads(resp.body)))
        except ValueError:
            return resp
    return resp


@router.get("/v1/parse/jobs", response_model=JobListOut, summary="任务列表")
async def list_jobs(request: Request, _u=Depends(require_role("super_admin", "admin"))) -> Response:
    engine, base = _engine(request)
    if engine == "cloud":
        return JSONResponse(mineru_saas.saas_list_jobs())
    return await _local_forward(request, base, "v1/parse/jobs")


@router.get("/v1/parse/jobs/{job_id}", response_model=JobOut, summary="轮询任务")
async def get_job(request: Request, job_id: str,
                  _u=Depends(require_role("super_admin", "admin"))) -> Response:
    engine, base = _engine(request)
    if engine == "cloud":
        return JSONResponse(await mineru_saas.saas_get_job(job_id))
    resp = await _local_forward(request, base, f"v1/parse/jobs/{job_id}")
    if resp.status_code == 200:
        try:
            return JSONResponse(_norm_job(json.loads(resp.body)))
        except ValueError:
            return resp
    return resp


@router.delete("/v1/parse/jobs/{job_id}", response_model=OkOut, summary="取消任务（云端不支持）")
async def delete_job(request: Request, job_id: str,
                     _u=Depends(require_role("super_admin", "admin"))) -> Response:
    engine, base = _engine(request)
    if engine == "cloud":
        mineru_saas.saas_delete_job(job_id)  # 必抛 409
        return JSONResponse({"ok": True})
    return await _local_forward(request, base, f"v1/parse/jobs/{job_id}")


@router.get("/v1/parse/jobs/{job_id}/files/{file_index}/result-zip",
            summary="下载原始结果 zip（full.md + images/，仅云端）")
async def result_zip(request: Request, job_id: str, file_index: int,
                     _u=Depends(require_role("super_admin", "admin"))) -> Response:
    engine, _base = _engine(request)
    if engine == "cloud":
        return await mineru_saas.saas_result_zip(job_id, file_index)
    raise HTTPException(404, "本地引擎 markdown 自包含图片（base64 内联），直接下载 .md 即可")


@router.get("/v1/files/{file_id:path}/content", summary="下载产物（markdown/structured_json）")
async def file_content(request: Request, file_id: str,
                       _u=Depends(require_role("super_admin", "admin"))) -> Response:
    engine, base = _engine(request)
    if engine == "cloud":
        return await mineru_saas.saas_file_content(file_id)
    return await _local_forward(request, base, f"v1/files/{file_id}/content")


# ---------- 兜底透传（本地 kit 私有端点；显式路由未覆盖时生效） ----------

@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"], include_in_schema=False)
async def proxy(path: str, request: Request, _u=Depends(require_role("super_admin", "admin"))) -> Response:
    # 尾斜杠归一化：显式路由（如 POST /v1/uploads）注册时不带尾斜杠，
    # 但 FastAPI 的 redirect_slashes 会被 path 转换器 catch-all 抢先匹配，
    # 导致 /v1/uploads/ 落到这里返回 404「云端 SaaS 模式未实现该端点」。
    # 这里手动 307 回无斜杠版本，让显式路由重新接管，避免调用方踩坑。
    if path.endswith("/") and len(path) > 1:
        stripped = path.rstrip("/")
        target = f"{request.url.path.rstrip('/')}"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return JSONResponse(
            status_code=307,
            content={"detail": f"路径尾斜杠已归一化，请重定向到 {stripped}"},
            headers={"Location": target},
        )

    engine, base = _engine(request)
    if engine == "cloud":
        raise HTTPException(404, f"云端 SaaS 模式未实现该端点：{request.method} /{path}")
    return await _local_forward(request, base, path)
