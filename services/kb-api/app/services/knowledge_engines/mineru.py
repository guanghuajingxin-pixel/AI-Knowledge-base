"""MinerU 解析引擎（本地 mineru-kit V1 API，默认 :8010）：文档库解析通道。

接口契约（mineru-kit V1 API，无鉴权）：
- 三步上传：POST /v1/uploads {filename,bytes,mime_type} →
  PUT /v1/uploads/{id}/content（裸字节）→ POST /v1/uploads/{id}/complete → file_id
- 提交解析：POST /v1/parse/jobs
  {files:[{source:{type:'file_id',file_id}}], output_formats:['markdown']} → job_id
- 查询任务：GET /v1/parse/jobs/{job_id}
  （status: queued|running|completed|partial|failed|canceled；
  产物引用在 files[].output_files.markdown.file_id）
- 下载产物：GET /v1/files/{file_id}/content（markdown 纯文本）
- 取消任务：DELETE /v1/parse/jobs/{job_id}
"""
import httpx

from . import EngineError

_MIME_BY_EXT = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
    "html": "text/html",
    "md": "text/markdown",
    "txt": "text/plain",
}

# job status → (文档状态, 进度)；partial = 产出可用但有告警
_JOB_STATES = {
    "queued": ("PARSING", 0.05),
    "running": ("PARSING", 0.5),
    "completed": ("COMPLETED", 1.0),
    "partial": ("COMPLETED", 1.0),
    "failed": ("FAILED", 0.0),
    "canceled": ("CANCELLED", 0.0),
}


def job_state(job: dict) -> tuple[str, float]:
    return _JOB_STATES.get(str(job.get("status", "")).lower(), ("UNKNOWN", 0.0))


class MinerUEngine:
    """文档库解析引擎：只负责「上传文件 / 建任务 / 查任务 / 取产物 / 取消」；
    分段与检索由本地完成（local_chunker + LibraryChunk）。"""

    def __init__(self, base_url: str):
        self.base_url = (base_url or "").strip().rstrip("/")
        if not self.base_url:
            raise EngineError("未配置 MinerU 解析服务地址（STRUCTURED_KIT_BASE_URL）")

    def _client(self):
        return httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=120.0, write=300.0, pool=10.0))

    async def upload(self, name: str, content: bytes) -> str:
        """三步上传，返回可提交解析的 file_id。"""
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        async with self._client() as c:
            r = await c.post(f"{self.base_url}/v1/uploads", json={
                "filename": name, "bytes": len(content),
                "mime_type": _MIME_BY_EXT.get(ext, "application/octet-stream"),
                "purpose": "parse",
            })
            if r.status_code not in (200, 201):
                raise EngineError(f"MinerU 创建上传失败：HTTP {r.status_code} {r.text[:200]}")
            upload_id = (r.json() or {}).get("id")
            if not upload_id:
                raise EngineError("MinerU 创建上传未返回 id")

            pu = await c.put(f"{self.base_url}/v1/uploads/{upload_id}/content",
                             content=content, headers={"Content-Type": "application/octet-stream"})
            if pu.status_code not in (200, 201, 204):
                raise EngineError(f"MinerU 上传字节失败：HTTP {pu.status_code} {pu.text[:200]}")

            cr = await c.post(f"{self.base_url}/v1/uploads/{upload_id}/complete")
            if cr.status_code not in (200, 201):
                raise EngineError(f"MinerU 完成上传失败：HTTP {cr.status_code} {cr.text[:200]}")
            # complete 返回 UploadResponse，真正的 File id 在嵌套 file 对象里
            file_id = ((cr.json() or {}).get("file") or {}).get("id")
            if not file_id:
                gr = await c.get(f"{self.base_url}/v1/uploads/{upload_id}")
                file_id = ((gr.json() or {}).get("file") or {}).get("id")
            if not file_id:
                raise EngineError("MinerU 完成上传后未取到 file id")
            return file_id

    async def create_job(self, file_id: str) -> str:
        async with self._client() as c:
            r = await c.post(f"{self.base_url}/v1/parse/jobs", json={
                "files": [{"source": {"type": "file_id", "file_id": file_id}}],
                "tier": "standard",
                # markdown 独立产物的图片是 base64 内联（撑爆存储且会被清洗剔除）；
                # zip 产物含 images/ 独立图片文件 + 相对引用 markdown，分段渲染图片依赖它
                "output_formats": ["markdown", "zip"],
            })
            if r.status_code not in (200, 201, 202):
                raise EngineError(f"MinerU 提交解析失败：HTTP {r.status_code} {r.text[:200]}")
            job_id = (r.json() or {}).get("job_id")
            if not job_id:
                raise EngineError("MinerU 未返回 job_id")
            return job_id

    async def job(self, job_id: str) -> dict:
        async with self._client() as c:
            r = await c.get(f"{self.base_url}/v1/parse/jobs/{job_id}")
            if r.status_code == 404:
                raise EngineError("解析任务不存在（可能已被清理），请重新解析")
            if r.status_code != 200:
                raise EngineError(f"MinerU 查询任务失败：HTTP {r.status_code} {r.text[:200]}")
            return r.json() or {}

    async def markdown(self, job: dict) -> str:
        """下载 job 首个文件的 markdown 产物。"""
        outputs = ((job.get("files") or [{}])[0].get("output_files") or {})
        file_id = (outputs.get("markdown") or {}).get("file_id")
        if not file_id:
            raise EngineError("解析完成但未返回 markdown 产物")
        async with self._client() as c:
            r = await c.get(f"{self.base_url}/v1/files/{file_id}/content")
            if r.status_code != 200:
                raise EngineError(f"MinerU 下载产物失败：HTTP {r.status_code}")
            try:
                data = r.json()
                return data if isinstance(data, str) else r.text
            except ValueError:
                return r.text

    async def zip_bundle(self, job: dict) -> bytes:
        """下载 job 首个文件的 zip 产物（含 images/ 独立图片与相对引用 markdown）。"""
        outputs = ((job.get("files") or [{}])[0].get("output_files") or {})
        file_id = (outputs.get("zip") or {}).get("file_id")
        if not file_id:
            raise EngineError("解析完成但未返回 zip 产物")
        async with self._client() as c:
            r = await c.get(f"{self.base_url}/v1/files/{file_id}/content")
            if r.status_code != 200:
                raise EngineError(f"MinerU 下载 zip 产物失败：HTTP {r.status_code}")
            return r.content

    async def cancel(self, job_id: str) -> None:
        """取消任务；任务已结束（404/409）不视为错误。"""
        async with self._client() as c:
            r = await c.delete(f"{self.base_url}/v1/parse/jobs/{job_id}")
            if r.status_code not in (200, 202, 204, 404, 409):
                raise EngineError(f"MinerU 取消任务失败：HTTP {r.status_code} {r.text[:200]}")


async def _saas(coro):
    """await 一个 mineru_saas 协程，把其 HTTPException 统一收敛为 EngineError，
    以便上层 invoke()/_finalize_parse 的 EngineError 分支（502 / 回退 markdown）生效。"""
    from fastapi import HTTPException
    try:
        return await coro
    except HTTPException as exc:  # saas 适配器用 HTTPException 表达业务错误
        raise EngineError(f"MinerU 云解析：{exc.detail}") from exc


class MinerUCloudEngine:
    """文档库解析引擎 · 云端 SaaS 通道（本地 mineru-kit 未部署时使用）。

    复用「解析引擎测试台」的 mineru_saas 适配器（mineru.net V4 契约，API Key 取
    settings 表 > .env），对外暴露与 MinerUEngine 完全一致的
    upload / create_job / job / markdown / zip_bundle / cancel 接口，
    使文档库解析流水线（_do_parse 提交 → 前端轮询 refresh → _finalize_parse 落地）
    无差别切换到云端。

    进程内临时态：SaaS 上传会话 / 任务注册表 / 结果 zip 缓存都存活在 kb-api 进程内。
    parse 提交与 refresh 轮询同在 kb-api，跨请求可见；kb-api 重启后进行中的云任务
    注册表清空，需重新解析（与测试台一致的既有限制）。
    """

    label = "cloud"

    async def upload(self, name: str, content: bytes) -> str:
        """三步上传（create → PUT 裸字节 → complete），返回可提交解析的 file_id。"""
        from app.routes import mineru_saas
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        meta = {"filename": name, "bytes": len(content),
                "mime_type": _MIME_BY_EXT.get(ext, "application/octet-stream")}
        up = await _saas(mineru_saas.saas_create_upload(meta))
        upload_id = (up or {}).get("id")
        if not upload_id:
            raise EngineError("MinerU 云创建上传未返回 id")
        await _saas(mineru_saas.saas_put_upload_content(upload_id, content))
        done = await _saas(mineru_saas.saas_complete_upload(upload_id))
        return ((done or {}).get("file") or {}).get("id") or upload_id

    async def create_job(self, file_id: str) -> str:
        from app.routes import mineru_saas
        brief = await _saas(mineru_saas.saas_create_job({
            "files": [{"source": {"type": "file_id", "file_id": file_id}}],
            "tier": "balanced",
            "output_formats": ["markdown", "zip"],
        }))
        job_id = (brief or {}).get("job_id")
        if not job_id:
            raise EngineError("MinerU 云未返回 job_id")
        return job_id

    async def job(self, job_id: str) -> dict:
        from app.routes import mineru_saas
        return await _saas(mineru_saas.saas_get_job(job_id)) or {}

    async def markdown(self, job: dict) -> str:
        from app.routes import mineru_saas
        outputs = ((job.get("files") or [{}])[0].get("output_files") or {})
        file_id = (outputs.get("markdown") or {}).get("file_id")
        if not file_id:
            raise EngineError("解析完成但未返回 markdown 产物")
        resp = await _saas(mineru_saas.saas_file_content(file_id))
        return resp.body.decode("utf-8", errors="replace")

    async def zip_bundle(self, job: dict) -> bytes:
        """结果 zip（full.md + images/），供分段渲染图片；不可用时上层回退 markdown。"""
        from app.routes import mineru_saas
        job_id = job.get("job_id")
        if not job_id:
            raise EngineError("云任务缺少 job_id，无法下载结果包")
        resp = await _saas(mineru_saas.saas_result_zip(job_id, 0))
        return resp.body

    async def cancel(self, job_id: str) -> None:
        """云端 SaaS 不支持取消；stop 端点已重置本地文档状态，这里静默返回。"""
        return None
