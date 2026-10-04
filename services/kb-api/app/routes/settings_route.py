import asyncio
import time
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from kb_common.database import get_session
from kb_common.models import Setting, DifyProfile, LLMProfile, RerankProfile, RagflowProfile, EmbeddingProfile
from app.deps import require_role, get_principal, get_current_user
from kb_common.config import get_settings
from kb_common.clients import llm_client

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])

# 可配置项白名单（is_secret=true 的只回显 key，不回显 value）
KEYS = {
    "site_name": ("站点名称（侧边栏 Logo 文字）", False),
    "site_logo": ("站点图标（图片 URL 或 data: 图片，替换侧边栏默认图标）", False),
    "llm_base_url": ("LLM 服务地址", False),
    "llm_api_key": ("LLM API Key", True),
    "llm_model": ("LLM 模型名", False),
    "llm_provider": ("LLM 供应商 (deepseek/zhipu/custom)", False),
    "mineru_api_key": ("MinerU API Key", True),
    "dify_base_url": ("Dify 服务地址", False),
    "dify_api_key": ("Dify API Key", True),
    "dify_upload_max_mb": ("Dify 单文件上传上限（MB）", False),
    "ragflow_base_url": ("RAGFlow 服务地址", False),
    "ragflow_api_key": ("RAGFlow API Key", True),
    "embedding_base_url": ("Embedding 服务地址（OpenAI 兼容 /embeddings，需含 /v1）", False),
    "embedding_api_key": ("Embedding API Key", True),
    "embedding_model": ("Embedding 模型名（如 BAAI/bge-m3）", False),
    "rerank_api_url": ("Rerank 服务地址（如 https://api.siliconflow.cn/v1/rerank）", False),
    "rerank_api_key": ("Rerank API Key", True),
    "rerank_model": ("Rerank 模型名（如 BAAI/bge-reranker-v2-m3）", False),
    "dingtalk_app_key": ("钉钉 AppKey", False),
    "dingtalk_app_secret": ("钉钉 AppSecret", True),
    "dingtalk_operator_union_id": ("钉钉服务账号 UnionId（兜底：同步源 owner 未绑定钉钉时使用）", False),
    "dingtalk_qr_login_enabled": ("钉钉扫码登录开关（true/false，缺省 true）", False),
    "dingtalk_robot_code": ("钉钉机器人 robotCode（发知识缺口通知）", False),
    "dingtalk_corp_id": ("钉钉 corpId（H5 免登）", False),
    "dingtalk_bot_enabled": ("钉钉机器人开关（true/false）", False),
    "dingtalk_bot_allow_users": ("钉钉机器人白名单（userid 逗号分隔，空=全员）", False),
}


def _mask_secret(val: str) -> str:
    """将密钥掩码为 sk-****3456 形式，保留首尾用于辨识。"""
    if not val:
        return ""
    if len(val) <= 8:
        return "****"
    return val[:3] + "****" + val[-4:]


@router.get("")
async def get_settings_api(u=Depends(require_role("super_admin", "admin")),
                           s: AsyncSession = Depends(get_session)):
    out = {}
    for k, (label, secret) in KEYS.items():
        row = (await s.execute(select(Setting).where(Setting.key == k))).scalar_one_or_none()
        val = row.value if row else getattr(get_settings(), k, "")
        out[k] = {"label": label, "value": _mask_secret(val) if (secret and val) else str(val),
                  "is_set": bool(val), "is_secret": secret}
    return out


class SettingIn(BaseModel):
    key: str
    value: str


@router.put("")
async def set_settings_api(body: SettingIn, u=Depends(require_role("super_admin", "admin")),
                          s: AsyncSession = Depends(get_session)):
    if body.key not in KEYS:
        raise HTTPException(400, "不支持的配置项")
    if body.key == "dify_upload_max_mb":
        try:
            size = int(body.value)
            if not 1 <= size <= 1024:
                raise ValueError()
        except ValueError:
            raise HTTPException(422, "上传上限必须为 1～1024 MB 的整数")
    if body.key == "site_name":
        if len(body.value) > 30:
            raise HTTPException(422, "站点名称最多 30 个字符")
    if body.key == "site_logo":
        v = body.value.strip()
        if v and not v.startswith(("http://", "https://", "data:image/")):
            raise HTTPException(422, "站点图标仅支持 http(s) 链接或 data: 图片")
        if len(v) > 512_000:
            raise HTTPException(422, "站点图标过大（超过 512KB）")
    secret = KEYS[body.key][1]
    # 密钥字段提交掩码值（含 ****）时视为未修改，保留原值
    if secret and "****" in body.value:
        return {"ok": True, "masked": True}
    row = (await s.execute(select(Setting).where(Setting.key == body.key))).scalar_one_or_none()
    if row:
        row.value = body.value
    else:
        s.add(Setting(key=body.key, value=body.value, is_secret=secret))
    await s.commit()
    if body.key.startswith("dingtalk_"):
        import asyncio
        from kb_common.clients import dingtalk_client
        from app.services import dingtalk_bot
        # 凭证改动立即生效：先失效运行时配置缓存再重新载入（否则会被 TTL 缓存挡住）
        dingtalk_client.invalidate_runtime_config()
        await dingtalk_client.sync_runtime_config()
        asyncio.create_task(dingtalk_bot.refresh_bot())
    if body.key in ("dify_base_url", "dify_api_key"):
        # 改了 Dify 链接/密钥：数据集列表缓存立即失效，避免最多吃 60s 旧列表
        from app.routes.dify_route import invalidate_datasets_cache
        invalidate_datasets_cache()
    return {"ok": True}


@router.get("/dingtalk-bot-status")
async def dingtalk_bot_status(u=Depends(require_role("super_admin", "admin"))):
    """钉钉机器人 Stream 运行状态（系统配置页展示：启用/连接/最近错误）。"""
    from app.services import dingtalk_bot
    return dingtalk_bot.get_status()


@router.get("/site")
async def get_site_branding(u=Depends(get_current_user),
                            s: AsyncSession = Depends(get_session)):
    """站点外观（所有登录用户可读）：侧边栏 Logo 名称与图标。"""
    out = {"site_name": "", "site_logo": ""}
    for k in out:
        row = (await s.execute(select(Setting).where(Setting.key == k))).scalar_one_or_none()
        out[k] = (row.value or "").strip() if row else ""
    return out


class TestLLMIn(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    profile_id: str = ""   # 编辑弹窗未重填 Key 时，回退该 llm_profile 已保存的 Key


@router.post("/test-llm")
async def test_llm_api(body: TestLLMIn, u=Depends(require_role("super_admin", "admin")),
                       s: AsyncSession = Depends(get_session)):
    """LLM 连通性测试：用弹窗中填写的参数发一次最小请求。

    编辑模型配置弹窗带 profile_id：api_key/model 留空时回退到该配置
    已保存的 Key 与默认模型（密钥不回显，测试无需重复输入）。
    无 profile_id 时回退到旧 settings 中的全局 LLM 密钥。
    """
    api_key = body.api_key.strip()
    base_url = body.base_url.strip()
    model = body.model.strip()

    profile = None
    if body.profile_id.strip():
        try:
            profile = await s.get(LLMProfile, uuid.UUID(body.profile_id.strip()))
        except (ValueError, TypeError):
            profile = None
    if profile is not None:
        if not api_key:
            api_key = profile.api_key
        if not base_url:
            base_url = (profile.base_url or "").strip()
        if not model:
            # 默认模型优先，其次第一个生效模型
            entries = [m for m in _parse_models(profile.models) if m.get("enabled")]
            default_entry = next((m for m in entries if m.get("is_default")), None)
            model = ((default_entry or (entries[0] if entries else {})).get("name") or "")

    base_url = (base_url or getattr(get_settings(), "llm_base_url", "")).strip()
    model = (model or getattr(get_settings(), "llm_model", "")).strip()
    if not api_key:
        row = (await s.execute(select(Setting).where(Setting.key == "llm_api_key"))).scalar_one_or_none()
        api_key = (row.value if row else "") or getattr(get_settings(), "llm_api_key", "")
    if not base_url:
        return {"ok": False, "message": "请先填写服务地址"}
    if not model:
        return {"ok": False, "message": "请先填写模型名"}
    if not api_key:
        return {"ok": False, "message": "请先填写 API Key"}

    t0 = time.perf_counter()
    try:
        reply = await asyncio.wait_for(
            llm_client.chat([{"role": "user", "content": "Hi, 请只回复两个字：pong"}],
                            model=model, base_url=base_url, api_key=api_key),
            timeout=20)
    except asyncio.TimeoutError:
        return {"ok": False, "message": "连接超时（20s），请检查服务地址与网络"}
    except Exception as e:
        return {"ok": False, "message": _friendly_llm_error(e)}
    latency_ms = int((time.perf_counter() - t0) * 1000)
    return {"ok": True, "latency_ms": latency_ms, "model": model,
            "reply": (reply or "").strip()[:80]}


def _friendly_llm_error(e: Exception) -> str:
    """把 openai SDK 异常映射为可操作的中文提示。"""
    name = type(e).__name__
    status = getattr(e, "status_code", None)
    if name == "AuthenticationError" or status == 401:
        return "API Key 无效或已过期，请检查后重试"
    if name == "PermissionDeniedError" or status == 403:
        return "API Key 无此模型的访问权限"
    if name == "NotFoundError" or status == 404:
        return "服务地址或模型名不存在，请检查拼写（注意 /v1 后缀）"
    if name == "RateLimitError" or status == 429:
        return "请求触发限流（429），Key 可用但请稍后重试"
    if name == "BadRequestError" or status == 400:
        return f"请求被拒绝（400）：{e}"
    if name == "InternalServerError" or (isinstance(status, int) and 500 <= status < 600):
        return f"服务端错误（{status}），请检查服务地址是否正确"
    if name in ("APIConnectionError", "APITimeoutError", "ConnectError", "TimeoutError"):
        return "无法连接到服务地址（网络不通/超时/DNS 解析失败），请检查地址与网络"
    return f"连接失败：{e}"


class TestDingtalkIn(BaseModel):
    app_key: str = ""
    app_secret: str = ""
    operator_union_id: str = ""


async def _effective_setting(s: AsyncSession, key: str) -> str:
    row = (await s.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
    return (row.value if row else "") or getattr(get_settings(), key, "")


def _not_found_message(r: httpx.Response, model: str, endpoint_hint: str) -> str:
    """拆解 404 的两种成因：模型名不存在 vs 端点路径不对。

    vLLM 等 OpenAI 兼容服务对未知模型同样返回 404，响应体形如
    {"error": {"message": "The model `X` does not exist.", "type": "NotFoundError"}}。
    旧实现把所有 404 一律报「端点不存在，请检查地址」，实测会把「模型名写错」
    （如服务端只有 Qwen3-Embedding-0.6B 却填了 4B）误导成地址问题，排查方向全错。
    """
    try:
        payload = r.json()
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            detail = str(err.get("message") or err.get("type") or "")
        elif isinstance(payload, dict):
            detail = str(payload.get("message") or payload.get("detail") or "")
        else:
            detail = ""
    except Exception:
        detail = ""
    detail = detail.strip() or (r.text or "")[:200].strip()
    low = detail.lower()
    if "model" in low and ("does not exist" in low or "not found" in low or "不存在" in low):
        return (f"模型名 `{model}` 在服务端不存在：{detail[:160]}。"
                f"请核对同一地址下 /v1/models 返回的实际模型 ID（命名空间与大小写需完全一致）")
    suffix = f"服务端返回：{detail[:160]}" if detail else "服务端无响应体"
    return f"端点不存在（404）：{endpoint_hint}。{suffix}"


@router.post("/test-dingtalk")
async def test_dingtalk_api(body: TestDingtalkIn, u=Depends(require_role("super_admin", "admin")),
                            s: AsyncSession = Depends(get_session)):
    """钉钉连通性测试：两步验证。

    1) 用 AppKey/AppSecret 换 accessToken（验证应用凭证）
    2) 用操作人 UnionId 拉取可见知识库列表（验证 unionId 与知识库读权限）
    Secret 留空时回退已配置值（不回显，测试无需重复输入）。
    """
    from kb_common.clients import dingtalk_client

    app_key = (body.app_key or await _effective_setting(s, "dingtalk_app_key")).strip()
    app_secret = (body.app_secret or await _effective_setting(s, "dingtalk_app_secret")).strip()
    union_id = (body.operator_union_id or await _effective_setting(s, "dingtalk_operator_union_id")).strip()
    if not app_key or not app_secret:
        return {"ok": False, "message": "请先填写 AppKey 与 AppSecret"}
    if not union_id:
        return {"ok": False, "message": "请先填写操作人 UnionId"}

    # 用测试参数临时覆盖运行时配置。注意必须走 dingtalk_client.configure() 注入：
    # _cfg() 的优先级是 _injected(DB) > .env，直接改 get_settings() 会被 DB 里的
    # 旧凭证挡住（曾导致“改配置→测试仍用旧应用→403→无法保存”的死锁）。
    # configure() 在凭证变化时会同时清 token / 知识库列表缓存，避免复用旧 token。
    old_injected = {k: dingtalk_client._injected.get(k, "") for k in
                    ("dingtalk_app_key", "dingtalk_app_secret", "dingtalk_operator_union_id")}
    dingtalk_client.configure(app_key, app_secret, union_id)
    t0 = time.perf_counter()
    try:
        await dingtalk_client._get_access_token()
        workspaces = await dingtalk_client.list_workspaces()
    except httpx.HTTPStatusError as e:
        return {"ok": False, "message": _friendly_dingtalk_error(e)}
    except RuntimeError as e:
        return {"ok": False, "message": str(e)}
    except Exception as e:
        return {"ok": False, "message": f"连接失败：{e}"}
    finally:
        # 恢复原注入值：configure() 忽略空串，空值需直接删键，避免测试值残留
        for k, v in old_injected.items():
            if v:
                dingtalk_client._injected[k] = v
            else:
                dingtalk_client._injected.pop(k, None)
        dingtalk_client._token_cache["token"] = ""
        dingtalk_client._token_cache["expire_at"] = 0.0
        dingtalk_client.invalidate_workspaces_cache()
    latency_ms = int((time.perf_counter() - t0) * 1000)
    return {"ok": True, "latency_ms": latency_ms, "workspace_count": len(workspaces),
            "message": f"验证通过，操作人可见 {len(workspaces)} 个知识库"}


def _friendly_dingtalk_error(e: httpx.HTTPStatusError) -> str:
    """把钉钉 API HTTP 错误映射为可操作的中文提示。"""
    status = e.response.status_code
    detail = ""
    try:
        data = e.response.json()
        detail = data.get("message") or data.get("errmsg") or ""
    except Exception:
        pass
    if status in (401, 403):
        return f"鉴权失败（{status}）：请检查 AppKey/AppSecret 与应用权限。{detail}"
    if "unionId" in detail or "operator" in detail.lower():
        return f"操作人 UnionId 无效或无知识库读权限：{detail}"
    if status == 400:
        return f"请求被拒绝（400）：{detail or '请检查 AppKey/AppSecret 是否正确'}"
    if status >= 500:
        return f"钉钉服务端错误（{status}），请稍后重试"
    return f"连接失败（{status}）：{detail or e}"


# ============ Dify 多配置（多条只能生效一条） ============

class TestDifyIn(BaseModel):
    base_url: str = ""
    api_key: str = ""
    profile_id: str = ""   # 编辑态 Key 为掩码时，回退该 profile 已存 Key


@router.post("/test-dify")
async def test_dify_api(body: TestDifyIn, u=Depends(require_role("super_admin", "admin")),
                        s: AsyncSession = Depends(get_session)):
    """Dify 连通性测试：用给定端点+Key 调 /datasets，验证地址与 Key 有效性。"""
    base_url = body.base_url.strip().rstrip("/")
    api_key = body.api_key.strip()
    if (not api_key or "****" in api_key) and body.profile_id:
        try:
            pid = uuid.UUID(body.profile_id)
            row = (await s.execute(select(DifyProfile).where(DifyProfile.id == pid))).scalar_one_or_none()
            if row and row.api_key:
                api_key = row.api_key
        except ValueError:
            pass
    if not api_key:
        api_key = await _effective_setting(s, "dify_api_key")
    if not base_url:
        return {"ok": False, "message": "请先填写服务地址"}
    if not api_key:
        return {"ok": False, "message": "请先填写 API Key"}

    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{base_url}/datasets",
                            headers={"Authorization": f"Bearer {api_key}"},
                            params={"page": 1, "limit": 1})
    except Exception:
        return {"ok": False, "message": "无法连接到服务地址（网络不通/超时/DNS 失败），请检查地址"}
    if r.status_code == 401:
        return {"ok": False, "message": "API Key 无效或已过期，请检查后重试"}
    if r.status_code == 403:
        return {"ok": False, "message": "API Key 无访问知识库权限"}
    if r.status_code == 404:
        return {"ok": False, "message": "端点不存在，请检查地址（需含端口与 /v1，如 http://127.0.0.1:8088/v1）"}
    if r.status_code >= 500:
        return {"ok": False, "message": f"Dify 服务端错误（{r.status_code}），请检查服务状态"}
    if r.status_code != 200:
        return {"ok": False, "message": f"连接失败（{r.status_code}）：{r.text[:120]}"}
    total = (r.json() or {}).get("total")
    latency_ms = int((time.perf_counter() - t0) * 1000)
    return {"ok": True, "latency_ms": latency_ms,
            "message": f"连接成功，可见 {total if total is not None else '?'} 个数据集"}


class TestMinerUIn(BaseModel):
    api_key: str = ""


# 非对话类模型关键词：智能问答只展示推理模型，embedding/rerank/语音图像等不出现
_LLM_MODEL_EXCLUDE = (
    "embedding", "-emb", "rerank", "whisper", "tts", "stt", "asr",
    "dall-e", "moderation", "image", "audio", "speech", "clip", "bge-",
    "text-embedding", "instruct-embedding", "sdxl", "stable-diffusion",
)


@router.get("/llm-models")
async def list_llm_models(base_url: str = "", api_key: str = "",
                          u=Depends(require_role("super_admin", "admin")),
                          s: AsyncSession = Depends(get_session)):
    """拉取模型供应商可用模型列表（OpenAI 兼容 /models），仅返回推理类模型。"""
    base_url = base_url.strip().rstrip("/")
    api_key = api_key.strip()
    if not api_key:
        api_key = await _effective_setting(s, "llm_api_key")
    if not base_url or not api_key:
        return {"ok": False, "models": [], "message": "请先填写服务地址与 API Key"}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{base_url}/models", headers={"Authorization": f"Bearer {api_key}"})
    except Exception:
        return {"ok": False, "models": [], "message": "无法连接到服务地址"}
    if r.status_code in (401, 403):
        return {"ok": False, "models": [], "message": "API Key 无效或已过期"}
    if r.status_code != 200:
        return {"ok": False, "models": [], "message": f"获取模型列表失败（{r.status_code}）"}
    try:
        data = r.json()
        ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
    except Exception:
        return {"ok": False, "models": [], "message": "响应格式异常"}
    models = sorted(i for i in ids if not any(x in i.lower() for x in _LLM_MODEL_EXCLUDE))
    return {"ok": True, "models": models}


@router.get("/external-models")
async def list_external_models(base_url: str = "", api_key: str = "",
                               kind: str = "", profile_id: str = "",
                               u=Depends(require_role("super_admin", "admin")),
                               s: AsyncSession = Depends(get_session)):
    """拉取 OpenAI 兼容服务的全部模型列表（不过滤）。

    供 Embedding / Rerank 配置弹窗「拉取模型列表」使用——这两类模型名
    （bge-m3、bge-reranker 等）恰在 llm-models 的排除词内，故单独提供。
    编辑态传 kind+profile_id：Key 未重填时回退该 profile 已存 Key。
    """
    base_url = base_url.strip().rstrip("/")
    api_key = api_key.strip()
    if profile_id and (not api_key or "****" in api_key):
        pid = _safe_uuid(profile_id)
        if kind == "rerank":
            row = (await s.execute(select(RerankProfile).where(RerankProfile.id == pid))).scalar_one_or_none()
        elif kind == "embedding":
            row = (await s.execute(select(EmbeddingProfile).where(EmbeddingProfile.id == pid))).scalar_one_or_none()
        else:
            row = None
        if row and row.api_key:
            api_key = row.api_key
    if not base_url:
        return {"ok": False, "models": [], "message": "请先填写服务地址"}
    try:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{base_url}/models", headers=headers)
    except Exception:
        return {"ok": False, "models": [], "message": "无法连接到服务地址"}
    if r.status_code in (401, 403):
        return {"ok": False, "models": [], "message": "API Key 无效或已过期"}
    if r.status_code != 200:
        return {"ok": False, "models": [], "message": f"获取模型列表失败（{r.status_code}）"}
    try:
        data = r.json()
        ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
    except Exception:
        return {"ok": False, "models": [], "message": "响应格式异常"}
    return {"ok": True, "models": sorted(ids)}


@router.post("/test-mineru")
async def test_mineru_api(body: TestMinerUIn, u=Depends(require_role("super_admin", "admin")),
                          s: AsyncSession = Depends(get_session)):
    """MinerU 连通性测试：用 Key 申请一个上传批次（不传文件、即弃），验证 Key 有效性。"""
    api_key = (body.api_key or await _effective_setting(s, "mineru_api_key")).strip()
    if not api_key:
        return {"ok": False, "message": "请先填写 API Key"}
    base = get_settings().mineru_api_url.rstrip("/")
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{base}/file-urls/batch",
                             headers={"Authorization": f"Bearer {api_key}"},
                             json={"enable_formula": False, "enable_table": False,
                                   "language": "ch", "files": [{"name": "connectivity-test.pdf"}]})
    except Exception:
        return {"ok": False, "message": "无法连接到 MinerU 服务（网络不通/超时），请检查网络"}
    if r.status_code in (401, 403):
        return {"ok": False, "message": "API Key 无效或已过期，请检查后重试"}
    try:
        payload = r.json()
    except Exception:
        return {"ok": False, "message": f"响应异常（{r.status_code}）：{r.text[:120]}"}
    if r.status_code != 200 or payload.get("code") not in (0, "0", None):
        return {"ok": False, "message": f"连接失败（{r.status_code}）：{payload.get('msg') or r.text[:120]}"}
    latency_ms = int((time.perf_counter() - t0) * 1000)
    return {"ok": True, "latency_ms": latency_ms, "message": "Key 有效，云解析服务可用"}


class TestEmbeddingIn(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    profile_id: str = ""   # 编辑态 Key 为掩码/留空时，回退该 profile 已存值


@router.post("/test-embedding")
async def test_embedding_api(body: TestEmbeddingIn, u=Depends(require_role("super_admin", "admin")),
                              s: AsyncSession = Depends(get_session)):
    """Embedding 连通性测试：用填写的参数发一次最小 embed 请求。

    留空时回退已保存值（密钥不回显，测试无需重复输入）；编辑弹窗传 profile_id 时
    优先回退该 profile。Key 可选——内网/自建服务（如 Xinference）常免鉴权。
    """
    base_url = body.base_url.strip().rstrip("/")
    api_key = body.api_key.strip()
    model = body.model.strip()
    if body.profile_id:
        pid = _safe_uuid(body.profile_id)
        row = (await s.execute(select(EmbeddingProfile).where(EmbeddingProfile.id == pid))).scalar_one_or_none()
        if row:
            if not base_url:
                base_url = row.api_url
            if (not api_key or "****" in api_key) and row.api_key:
                api_key = row.api_key
            if not model:
                model = row.model
    if not base_url:
        base_url = await _effective_setting(s, "embedding_base_url")
    if not api_key or "****" in api_key:
        api_key = await _effective_setting(s, "embedding_api_key")
    if not model:
        model = await _effective_setting(s, "embedding_model")
    if not base_url:
        return {"ok": False, "message": "请先填写服务地址"}
    if not model:
        return {"ok": False, "message": "请先填写模型名"}
    t0 = time.perf_counter()
    try:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"{base_url}/embeddings",
                headers=headers,
                json={"model": model, "input": ["连通性测试"]},
            )
    except Exception:
        return {"ok": False, "message": "无法连接到服务地址（网络不通/超时/DNS 失败），请检查地址"}
    if r.status_code in (401, 403):
        return {"ok": False, "message": "API Key 无效或无访问权限"}
    if r.status_code == 404:
        return {"ok": False, "message": _not_found_message(
            r, model, "请检查地址（需含 /v1，末尾不带 /embeddings）")}
    if r.status_code >= 400:
        try:
            detail = r.json().get("message") or r.text[:120]
        except Exception:
            detail = r.text[:120]
        return {"ok": False, "message": f"请求失败（{r.status_code}）：{detail}"}
    try:
        dim = len(r.json()["data"][0]["embedding"])
    except Exception:
        return {"ok": False, "message": "响应格式异常，非标准 OpenAI embeddings 返回"}
    latency_ms = int((time.perf_counter() - t0) * 1000)
    return {"ok": True, "latency_ms": latency_ms, "dimension": dim,
            "message": f"连接成功，向量维度 {dim}"}


class TestRerankIn(BaseModel):
    api_url: str = ""
    api_key: str = ""
    model: str = ""
    profile_id: str = ""


@router.post("/test-rerank")
async def test_rerank_api(body: TestRerankIn, u=Depends(require_role("super_admin", "admin")),
                           s: AsyncSession = Depends(get_session)):
    """Rerank 连通性测试：用填写的参数发一次最小 rerank 请求。

    留空时回退已保存值（密钥不回显，测试无需重复输入）。
    API Key 非必填：内网/自建 rerank 服务（如 Xinference）常免鉴权。
    """
    api_url = body.api_url.strip()
    api_key = body.api_key.strip()
    model = body.model.strip()
    if body.profile_id:
        pid = _safe_uuid(body.profile_id)
        row = (await s.execute(select(RerankProfile).where(RerankProfile.id == pid))).scalar_one_or_none()
        if row:
            if not api_url:
                api_url = row.api_url
            if not api_key or "****" in api_key:
                api_key = row.api_key
            if not model:
                model = row.model
    if not api_url:
        api_url = await _effective_setting(s, "rerank_api_url")
    if not api_key or "****" in api_key:
        api_key = await _effective_setting(s, "rerank_api_key")
    if not model:
        model = await _effective_setting(s, "rerank_model")
    if not api_url:
        return {"ok": False, "message": "请先填写服务地址"}
    if not model:
        return {"ok": False, "message": "请先填写模型名"}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    test_docs = ["这是一条测试文档", "无关内容"]
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                api_url,
                headers=headers,
                # 与检索链路同构：显式传 top_n，避免「测试通过、实际请求被拒」
                json={"model": model, "query": "测试",
                      "documents": test_docs, "top_n": len(test_docs)},
            )
    except Exception:
        return {"ok": False, "message": "无法连接到服务地址（网络不通/超时/DNS 失败），请检查地址"}
    if r.status_code in (401, 403):
        return {"ok": False, "message": "API Key 无效或无访问权限"}
    if r.status_code == 404:
        return {"ok": False, "message": _not_found_message(
            r, model, "请检查地址（需含完整 /rerank 路径）")}
    if r.status_code >= 400:
        try:
            detail = r.json().get("message") or r.text[:120]
        except Exception:
            detail = r.text[:120]
        return {"ok": False, "message": f"请求失败（{r.status_code}）：{detail}"}
    try:
        results = r.json().get("results")
    except Exception:
        return {"ok": False, "message": "响应格式异常，非标准 rerank 返回"}
    # 与 apply_rerank 的解析口径一致：实际检索要求每条候选都返回
    # 0～1 的 relevance_score 且 index 唯一有效，测试阶段提前暴露不兼容服务
    if not isinstance(results, list) or len(results) != len(test_docs):
        return {"ok": False, "message": f"响应格式异常：应返回 {len(test_docs)} 条 results，实际 {len(results) if isinstance(results, list) else '非数组'}"}
    seen = set()
    for item in results:
        if not isinstance(item, dict):
            return {"ok": False, "message": "响应格式异常：results 条目结构不正确"}
        idx = item.get("index")
        score = item.get("relevance_score")
        if type(idx) is not int or not 0 <= idx < len(test_docs) or idx in seen:
            return {"ok": False, "message": "响应格式异常：index 无效或重复"}
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
            return {"ok": False, "message": "响应格式异常：relevance_score 需为 0～1 的数值"}
        seen.add(idx)
    latency_ms = int((time.perf_counter() - t0) * 1000)
    return {"ok": True, "latency_ms": latency_ms,
            "message": f"连接成功，返回 {len(results)} 条重排结果"}


def _mask_api_key(val: str) -> str:
    if not val:
        return ""
    if len(val) <= 8:
        return "****"
    return val[:3] + "****" + val[-4:]


def _safe_uuid(val: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(val))
    except (ValueError, AttributeError):
        raise HTTPException(400, "无效的配置 ID")


def _profile_to_dict(p: DifyProfile, mask: bool = True) -> dict:
    return {
        "id": str(p.id),
        "name": p.name,
        "base_url": p.base_url,
        "api_key": _mask_api_key(p.api_key) if mask else p.api_key,
        "dataset_ids": p.dataset_ids,
        "enabled": p.enabled,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


class DifyProfileIn(BaseModel):
    name: str
    base_url: str = ""
    api_key: str = ""
    dataset_ids: str = ""


@router.get("/dify-profiles")
async def list_dify_profiles(u=Depends(require_role("super_admin", "admin")),
                              s: AsyncSession = Depends(get_session)):
    await seed_profiles_from_legacy_settings(s)
    rows = (await s.execute(select(DifyProfile).order_by(DifyProfile.created_at))).scalars().all()
    return [_profile_to_dict(r) for r in rows]


@router.post("/dify-profiles")
async def create_dify_profile(body: DifyProfileIn,
                               u=Depends(require_role("super_admin", "admin")),
                               s: AsyncSession = Depends(get_session)):
    if not body.name.strip():
        raise HTTPException(400, "配置名称不能为空")
    profile = DifyProfile(
        name=body.name.strip(),
        base_url=body.base_url.strip(),
        api_key=body.api_key.strip(),
        dataset_ids=body.dataset_ids.strip(),
        enabled=False,
    )
    s.add(profile)
    await s.commit()
    await s.refresh(profile)
    return _profile_to_dict(profile)


@router.put("/dify-profiles/{profile_id}")
async def update_dify_profile(profile_id: str, body: DifyProfileIn,
                               u=Depends(require_role("super_admin", "admin")),
                               s: AsyncSession = Depends(get_session)):
    try:
        pid = uuid.UUID(profile_id)
    except ValueError:
        raise HTTPException(400, "无效的配置 ID")
    row = (await s.execute(select(DifyProfile).where(DifyProfile.id == pid))).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "配置不存在")
    row.name = body.name.strip()
    row.base_url = body.base_url.strip()
    # 密钥含 **** 视为未修改
    if body.api_key and "****" not in body.api_key:
        row.api_key = body.api_key.strip()
    row.dataset_ids = body.dataset_ids.strip()
    await s.commit()
    await s.refresh(row)
    return _profile_to_dict(row)


@router.delete("/dify-profiles/{profile_id}")
async def delete_dify_profile(profile_id: str,
                               u=Depends(require_role("super_admin", "admin")),
                               s: AsyncSession = Depends(get_session)):
    try:
        pid = uuid.UUID(profile_id)
    except ValueError:
        raise HTTPException(400, "无效的配置 ID")
    row = (await s.execute(select(DifyProfile).where(DifyProfile.id == pid))).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "配置不存在")
    was_enabled = row.enabled
    await s.delete(row)
    await s.commit()
    # 若删除的是启用项，自动启用第一条
    if was_enabled:
        first = (await s.execute(select(DifyProfile).order_by(DifyProfile.created_at).limit(1))).scalar_one_or_none()
        if first:
            first.enabled = True
            await s.commit()
    return {"ok": True}


@router.put("/dify-profiles/{profile_id}/enable")
async def enable_dify_profile(profile_id: str,
                               u=Depends(require_role("super_admin", "admin")),
                               s: AsyncSession = Depends(get_session)):
    """启用指定配置，同时禁用其他所有配置（只能生效一条）。"""
    try:
        pid = uuid.UUID(profile_id)
    except ValueError:
        raise HTTPException(400, "无效的配置 ID")
    row = (await s.execute(select(DifyProfile).where(DifyProfile.id == pid))).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "配置不存在")
    # 先禁用全部，再启用目标
    await s.execute(update(DifyProfile).values(enabled=False))
    row.enabled = True
    await s.commit()
    # 同步写入 settings 表，使 dify_client 能立即读取
    # 注意：只同步「连接」（base_url/api_key）；具体检索哪些库由知识源注册表决定，
    # 不再回写 dify_dataset_ids（系统配置页已下线库列表）。
    from kb_common.models import Setting as SettingModel
    for k, v in [("dify_base_url", row.base_url), ("dify_api_key", row.api_key)]:
        existing = (await s.execute(select(SettingModel).where(SettingModel.key == k))).scalar_one_or_none()
        if existing:
            existing.value = v
        else:
            s.add(SettingModel(key=k, value=v, is_secret=(k == "dify_api_key")))
    await s.commit()
    # 切换 Dify 链接后数据集列表缓存必须失效，否则会按旧配置返回最长 60s 的旧列表
    from app.routes.dify_route import invalidate_datasets_cache
    invalidate_datasets_cache()
    return {"ok": True}


# ==================== LLM 供应商配置（多供应商 × 多模型） ====================
import json as _json

# 非对话类模型关键词：智能问答只展示推理模型
_LLM_MODEL_EXCLUDE_KW = (
    "embedding", "-emb", "rerank", "whisper", "tts", "stt", "asr",
    "dall-e", "moderation", "image", "audio", "speech", "clip", "bge-",
    "text-embedding", "instruct-embedding", "sdxl", "stable-diffusion",
)


def _parse_models(raw: str) -> list[dict]:
    try:
        data = _json.loads(raw or "[]")
        return [m for m in data if isinstance(m, dict) and m.get("name")]
    except Exception:
        return []


def _dump_models(models: list[dict]) -> str:
    clean = []
    for m in models:
        name = str(m.get("name", "")).strip()
        if not name:
            continue
        clean.append({
            "name": name,
            "enabled": bool(m.get("enabled")),
            "is_default": bool(m.get("is_default")),
        })
    return _json.dumps(clean, ensure_ascii=False)


def _infer_provider(base_url: str) -> str:
    """按服务地址推断供应商类型，用于旧配置迁移建档。"""
    b = (base_url or "").lower()
    if "deepseek" in b:
        return "deepseek"
    if "bigmodel" in b or "zhipu" in b:
        return "zhipu"
    return "custom"


async def _sync_legacy_llm_settings(s: AsyncSession) -> None:
    """将全局默认模型所属供应商回写旧版单值 settings（llm_base_url/llm_api_key/llm_model/llm_provider）。

    知识加工、agent 回调、问答兜底等链路仍读旧键；系统配置页保存 LLM 配置后
    同步旧键，保证运行时与页面配置始终一致。无全局默认模型时保持旧键不动。
    """
    rows = (await s.execute(select(LLMProfile))).scalars().all()
    chosen: tuple | None = None
    for p in rows:
        for m in _parse_models(p.models):
            if m.get("enabled") and m.get("is_default"):
                chosen = (p, m)
                break
        if chosen:
            break
    if chosen is None:
        return
    p, m = chosen
    for k, v in [("llm_base_url", p.base_url), ("llm_api_key", p.api_key),
                 ("llm_model", m["name"]), ("llm_provider", p.provider)]:
        row = (await s.execute(select(Setting).where(Setting.key == k))).scalar_one_or_none()
        if row:
            row.value = v
        else:
            s.add(Setting(key=k, value=v, is_secret=(k == "llm_api_key")))


async def seed_profiles_from_legacy_settings(s: AsyncSession) -> None:
    """一次性迁移：profiles 表为空时，用旧版单值设置生成对应 profile。

    模型配置页下线后，存量的 LLM / Dify 单值配置若不在 profiles 表中，
    将无法在系统配置页可见可管理；此处按「表空才迁移」幂等建档：
    - LLM：需地址+Key+模型名齐全，默认模型标记为生效+默认；
    - Dify：需地址存在，建档即生效（与迁移前运行时行为一致）。
    """
    if (await s.execute(select(LLMProfile).limit(1))).scalar_one_or_none() is None:
        base_url = await _effective_setting(s, "llm_base_url")
        api_key = await _effective_setting(s, "llm_api_key")
        model = await _effective_setting(s, "llm_model")
        if base_url and api_key and model:
            provider = _infer_provider(base_url)
            label = {"deepseek": "DeepSeek", "zhipu": "GLM（智谱）"}.get(provider, "自定义模型")
            s.add(LLMProfile(
                name=f"{label}·旧配置迁移",
                provider=provider,
                base_url=base_url.strip().rstrip("/"),
                api_key=api_key,
                models=_dump_models([{"name": model, "enabled": True, "is_default": True}]),
            ))
            await s.commit()
    if (await s.execute(select(DifyProfile).limit(1))).scalar_one_or_none() is None:
        base_url = await _effective_setting(s, "dify_base_url")
        if base_url:
            s.add(DifyProfile(
                name="默认·旧配置迁移",
                base_url=base_url.strip().rstrip("/"),
                api_key=await _effective_setting(s, "dify_api_key"),
                dataset_ids=await _effective_setting(s, "dify_dataset_ids"),
                enabled=True,
            ))
            await s.commit()


def _llm_to_dict(p: LLMProfile, mask: bool = True) -> dict:
    return {
        "id": str(p.id),
        "name": p.name,
        "provider": p.provider,
        "base_url": p.base_url,
        "api_key": _mask_api_key(p.api_key) if mask else p.api_key,
        "has_key": bool(p.api_key),
        "models": _parse_models(p.models),
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


async def _fetch_remote_models(base_url: str, api_key: str) -> list[str]:
    """调供应商 /models 拉取推理类模型 ID 列表。失败抛 HTTPException。"""
    base_url = base_url.strip().rstrip("/")
    if not base_url or not api_key:
        raise HTTPException(400, "请先填写服务地址与 API Key")
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{base_url}/models", headers={"Authorization": f"Bearer {api_key}"})
    except Exception:
        raise HTTPException(502, "无法连接到服务地址")
    if r.status_code in (401, 403):
        raise HTTPException(400, "API Key 无效或已过期")
    if r.status_code != 200:
        raise HTTPException(502, f"获取模型列表失败（{r.status_code}）")
    try:
        ids = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
    except Exception:
        raise HTTPException(502, "响应格式异常")
    return sorted(i for i in ids if not any(x in i.lower() for x in _LLM_MODEL_EXCLUDE_KW))


class LLMProfileIn(BaseModel):
    name: str
    provider: str = "custom"
    base_url: str = ""
    api_key: str = ""          # 留空且已配置 → 保留原 Key
    models: list[dict] = []


@router.get("/llm-profiles")
async def list_llm_profiles(u=Depends(require_role("super_admin", "admin")),
                            s: AsyncSession = Depends(get_session)):
    await seed_profiles_from_legacy_settings(s)
    rows = (await s.execute(select(LLMProfile).order_by(LLMProfile.created_at))).scalars().all()
    return [_llm_to_dict(r) for r in rows]


@router.post("/llm-profiles")
async def create_llm_profile(body: LLMProfileIn,
                             u=Depends(require_role("super_admin", "admin")),
                             s: AsyncSession = Depends(get_session)):
    if not body.name.strip():
        raise HTTPException(400, "配置名称不能为空")
    has_default = any(m.get("is_default") for m in body.models)
    profile = LLMProfile(
        name=body.name.strip(),
        provider=body.provider.strip() or "custom",
        base_url=body.base_url.strip().rstrip("/"),
        api_key=body.api_key.strip(),
        models=_dump_models(body.models),
    )
    s.add(profile)
    await s.flush()
    if has_default:
        await _clear_other_defaults(s, str(profile.id))
    await _sync_legacy_llm_settings(s)
    await s.commit()
    return {"ok": True, "id": str(profile.id)}


@router.put("/llm-profiles/{profile_id}")
async def update_llm_profile(profile_id: str, body: LLMProfileIn,
                             u=Depends(require_role("super_admin", "admin")),
                             s: AsyncSession = Depends(get_session)):
    pid = _safe_uuid(profile_id)
    row = (await s.execute(select(LLMProfile).where(LLMProfile.id == pid))).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "配置不存在")
    row.name = body.name.strip() or row.name
    row.provider = body.provider.strip() or "custom"
    row.base_url = body.base_url.strip().rstrip("/")
    # Key 含掩码或留空 → 保留原值
    new_key = body.api_key.strip()
    if new_key and "****" not in new_key:
        row.api_key = new_key
    if body.models:
        # 停用 → 强制取消默认；本配置内只允许一个默认
        default_seen = False
        for m in body.models:
            if not m.get("enabled"):
                m["is_default"] = False
            if m.get("is_default"):
                if default_seen:
                    m["is_default"] = False
                default_seen = True
        row.models = _dump_models(body.models)
        if default_seen:
            await _clear_other_defaults(s, profile_id)
    await _sync_legacy_llm_settings(s)
    await s.commit()
    return {"ok": True}


@router.delete("/llm-profiles/{profile_id}")
async def delete_llm_profile(profile_id: str,
                             u=Depends(require_role("super_admin", "admin")),
                             s: AsyncSession = Depends(get_session)):
    pid = _safe_uuid(profile_id)
    row = (await s.execute(select(LLMProfile).where(LLMProfile.id == pid))).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "配置不存在")
    await s.delete(row)
    await s.commit()
    await _sync_legacy_llm_settings(s)
    await s.commit()
    return {"ok": True}


@router.post("/llm-profiles/{profile_id}/refresh-models")
async def refresh_llm_profile_models(profile_id: str,
                                     u=Depends(require_role("super_admin", "admin")),
                                     s: AsyncSession = Depends(get_session)):
    """拉取供应商可用推理模型，与已勾选状态合并（保留 enabled/is_default）。"""
    pid = _safe_uuid(profile_id)
    row = (await s.execute(select(LLMProfile).where(LLMProfile.id == pid))).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "配置不存在")
    if not row.api_key:
        raise HTTPException(400, "请先保存 API Key")
    remote = await _fetch_remote_models(row.base_url, row.api_key)
    existing = {m["name"]: m for m in _parse_models(row.models)}
    merged: list[dict] = []
    for name in remote:
        old = existing.get(name, {})
        merged.append({
            "name": name,
            "enabled": bool(old.get("enabled", False)),
            "is_default": bool(old.get("is_default", False)),
        })
    # 远程已下线但本地曾配置的模型保留在末尾，避免静默丢失
    for name, old in existing.items():
        if name not in set(remote):
            merged.append({"name": name, "enabled": bool(old.get("enabled")),
                           "is_default": bool(old.get("is_default"))})
    row.models = _dump_models(merged)
    await s.commit()
    return {"ok": True, "models": merged}


async def _clear_other_defaults(s: AsyncSession, keep_profile_id: str) -> None:
    """全局默认互斥：清除其它供应商配置中模型的 is_default 标记。"""
    keep_pid = _safe_uuid(keep_profile_id)
    rows = (await s.execute(select(LLMProfile).where(LLMProfile.id != keep_pid))).scalars().all()
    for r in rows:
        models = _parse_models(r.models)
        changed = False
        for m in models:
            if m.get("is_default"):
                m["is_default"] = False
                changed = True
        if changed:
            r.models = _dump_models(models)


# ==================== Rerank 重排模型配置（多条只能生效一条） ====================

def _rerank_to_dict(p: RerankProfile, mask: bool = True) -> dict:
    return {
        "id": str(p.id),
        "name": p.name,
        "api_url": p.api_url,
        "api_key": _mask_api_key(p.api_key) if mask else p.api_key,
        "has_key": bool(p.api_key),
        "model": p.model,
        "enabled": p.enabled,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


async def _seed_rerank_profiles(s: AsyncSession) -> None:
    """一次性迁移：rerank_profiles 表为空且旧版单值已配置时，建档并标记生效。"""
    if (await s.execute(select(RerankProfile).limit(1))).scalar_one_or_none() is not None:
        return
    api_url = await _effective_setting(s, "rerank_api_url")
    if not api_url:
        return
    s.add(RerankProfile(
        name="默认·旧配置迁移",
        api_url=api_url.strip(),
        api_key=await _effective_setting(s, "rerank_api_key"),
        model=await _effective_setting(s, "rerank_model"),
        enabled=True,
    ))
    await s.commit()


async def _sync_rerank_settings(s: AsyncSession) -> None:
    """生效配置回写旧版单值 settings，reranker 运行链路（.env → settings 覆盖）保持兼容。

    无生效配置时清空旧键：检索跳过重排，与「未配置」语义一致。
    """
    row = (await s.execute(select(RerankProfile).where(RerankProfile.enabled == True))).scalar_one_or_none()  # noqa: E712
    values = ([("rerank_api_url", row.api_url), ("rerank_api_key", row.api_key),
               ("rerank_model", row.model)] if row
              else [("rerank_api_url", ""), ("rerank_api_key", ""), ("rerank_model", "")])
    for k, v in values:
        setting = (await s.execute(select(Setting).where(Setting.key == k))).scalar_one_or_none()
        if setting:
            setting.value = v
        else:
            s.add(Setting(key=k, value=v, is_secret=(k == "rerank_api_key")))


class RerankProfileIn(BaseModel):
    name: str
    api_url: str = ""
    api_key: str = ""
    model: str = ""


@router.get("/rerank-profiles")
async def list_rerank_profiles(u=Depends(require_role("super_admin", "admin")),
                               s: AsyncSession = Depends(get_session)):
    await _seed_rerank_profiles(s)
    rows = (await s.execute(select(RerankProfile).order_by(RerankProfile.created_at))).scalars().all()
    return [_rerank_to_dict(r) for r in rows]


@router.post("/rerank-profiles")
async def create_rerank_profile(body: RerankProfileIn,
                                u=Depends(require_role("super_admin", "admin")),
                                s: AsyncSession = Depends(get_session)):
    if not body.name.strip():
        raise HTTPException(400, "配置名称不能为空")
    if not body.api_url.strip():
        raise HTTPException(400, "服务地址不能为空")
    if not body.model.strip():
        raise HTTPException(400, "模型名不能为空")
    profile = RerankProfile(
        name=body.name.strip(),
        api_url=body.api_url.strip(),
        api_key=body.api_key.strip(),
        model=body.model.strip(),
        enabled=False,
    )
    s.add(profile)
    await s.commit()
    await s.refresh(profile)
    return _rerank_to_dict(profile)


@router.put("/rerank-profiles/{profile_id}")
async def update_rerank_profile(profile_id: str, body: RerankProfileIn,
                                u=Depends(require_role("super_admin", "admin")),
                                s: AsyncSession = Depends(get_session)):
    pid = _safe_uuid(profile_id)
    row = (await s.execute(select(RerankProfile).where(RerankProfile.id == pid))).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "配置不存在")
    if not body.name.strip():
        raise HTTPException(400, "配置名称不能为空")
    if not body.api_url.strip():
        raise HTTPException(400, "服务地址不能为空")
    if not body.model.strip():
        raise HTTPException(400, "模型名不能为空")
    row.name = body.name.strip()
    row.api_url = body.api_url.strip()
    row.model = body.model.strip()
    # Key 语义：新值非掩码 → 覆盖；留空/掩码 → 保留原值；"__CLEAR__" 哨兵 → 显式清空（免鉴权服务）
    new_key = body.api_key.strip()
    if new_key == "__CLEAR__":
        row.api_key = ""
    elif new_key and "****" not in new_key:
        row.api_key = new_key
    if row.enabled:
        await _sync_rerank_settings(s)
    await s.commit()
    await s.refresh(row)
    return _rerank_to_dict(row)


@router.delete("/rerank-profiles/{profile_id}")
async def delete_rerank_profile(profile_id: str,
                                u=Depends(require_role("super_admin", "admin")),
                                s: AsyncSession = Depends(get_session)):
    pid = _safe_uuid(profile_id)
    row = (await s.execute(select(RerankProfile).where(RerankProfile.id == pid))).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "配置不存在")
    was_enabled = row.enabled
    await s.delete(row)
    await s.commit()
    if was_enabled:
        # 删除生效配置后自动切到最早一条，避免检索链路突然失去重排
        first = (await s.execute(select(RerankProfile).order_by(RerankProfile.created_at).limit(1))).scalar_one_or_none()
        if first:
            first.enabled = True
        await _sync_rerank_settings(s)
        await s.commit()
    return {"ok": True}


class RerankEnableIn(BaseModel):
    # 缺省 true 兼容无 body 的旧调用；false = 关闭当前生效模型（全局不重排）
    enabled: bool = True


@router.put("/rerank-profiles/{profile_id}/enable")
async def enable_rerank_profile(profile_id: str,
                                body: RerankEnableIn = RerankEnableIn(),
                                u=Depends(require_role("super_admin", "admin")),
                                s: AsyncSession = Depends(get_session)):
    """启用指定配置为默认生效模型（同时禁用其他，只能生效一条）；

    enabled=false 仅允许关闭当前正生效的配置，关闭后全局检索跳过重排。
    """
    pid = _safe_uuid(profile_id)
    row = (await s.execute(select(RerankProfile).where(RerankProfile.id == pid))).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "配置不存在")
    if not body.enabled:
        if not row.enabled:
            raise HTTPException(400, "该配置当前未生效，无需关闭")
        row.enabled = False
    else:
        await s.execute(update(RerankProfile).values(enabled=False))
        row.enabled = True
    await _sync_rerank_settings(s)
    await s.commit()
    return {"ok": True}


# ==================== RAGFlow 知识库多配置（多环境切换，只能生效一条） ====================

def _ragflow_to_dict(p: RagflowProfile, mask: bool = True) -> dict:
    return {
        "id": str(p.id),
        "name": p.name,
        "base_url": p.base_url,
        "api_key": _mask_api_key(p.api_key) if mask else p.api_key,
        "has_key": bool(p.api_key),
        "enabled": p.enabled,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


async def _seed_ragflow_profiles(s: AsyncSession) -> None:
    """一次性迁移：ragflow_profiles 表为空且旧版单值已配置时，建档并标记生效。"""
    if (await s.execute(select(RagflowProfile).limit(1))).scalar_one_or_none() is not None:
        return
    base_url = await _effective_setting(s, "ragflow_base_url")
    if not base_url:
        return
    s.add(RagflowProfile(
        name="默认·旧配置迁移",
        base_url=base_url.strip(),
        api_key=await _effective_setting(s, "ragflow_api_key"),
        enabled=True,
    ))
    await s.commit()


async def _sync_ragflow_settings(s: AsyncSession) -> None:
    """生效配置回写旧版单值 settings，ragflow_route / ragflow_client / 知识源同步链路保持兼容。

    无生效配置时清空旧键：RAGFlow 检索/同步提示未配置。
    """
    row = (await s.execute(select(RagflowProfile).where(RagflowProfile.enabled == True))).scalar_one_or_none()  # noqa: E712
    values = ([("ragflow_base_url", row.base_url), ("ragflow_api_key", row.api_key)] if row
              else [("ragflow_base_url", ""), ("ragflow_api_key", "")])
    for k, v in values:
        setting = (await s.execute(select(Setting).where(Setting.key == k))).scalar_one_or_none()
        if setting:
            setting.value = v
        else:
            s.add(Setting(key=k, value=v, is_secret=(k == "ragflow_api_key")))


class RagflowProfileIn(BaseModel):
    name: str
    base_url: str = ""
    api_key: str = ""


@router.get("/ragflow-profiles")
async def list_ragflow_profiles(u=Depends(require_role("super_admin", "admin")),
                                s: AsyncSession = Depends(get_session)):
    await _seed_ragflow_profiles(s)
    rows = (await s.execute(select(RagflowProfile).order_by(RagflowProfile.created_at))).scalars().all()
    return [_ragflow_to_dict(r) for r in rows]


@router.post("/ragflow-profiles")
async def create_ragflow_profile(body: RagflowProfileIn,
                                 u=Depends(require_role("super_admin", "admin")),
                                 s: AsyncSession = Depends(get_session)):
    if not body.name.strip():
        raise HTTPException(400, "配置名称不能为空")
    if not body.base_url.strip():
        raise HTTPException(400, "服务地址不能为空")
    if not body.api_key.strip() or "****" in body.api_key:
        raise HTTPException(400, "API Key 不能为空（RAGFlow 接口均需 Bearer 认证）")
    profile = RagflowProfile(
        name=body.name.strip(),
        base_url=body.base_url.strip(),
        api_key=body.api_key.strip(),
        enabled=False,
    )
    s.add(profile)
    await s.commit()
    await s.refresh(profile)
    return _ragflow_to_dict(profile)


@router.put("/ragflow-profiles/{profile_id}")
async def update_ragflow_profile(profile_id: str, body: RagflowProfileIn,
                                 u=Depends(require_role("super_admin", "admin")),
                                 s: AsyncSession = Depends(get_session)):
    pid = _safe_uuid(profile_id)
    row = (await s.execute(select(RagflowProfile).where(RagflowProfile.id == pid))).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "配置不存在")
    if not body.name.strip():
        raise HTTPException(400, "配置名称不能为空")
    if not body.base_url.strip():
        raise HTTPException(400, "服务地址不能为空")
    row.name = body.name.strip()
    row.base_url = body.base_url.strip()
    # Key 语义：新值非掩码 → 覆盖；留空/掩码 → 保留原值
    new_key = body.api_key.strip()
    if new_key and "****" not in new_key:
        row.api_key = new_key
    if row.enabled:
        await _sync_ragflow_settings(s)
    await s.commit()
    await s.refresh(row)
    return _ragflow_to_dict(row)


@router.delete("/ragflow-profiles/{profile_id}")
async def delete_ragflow_profile(profile_id: str,
                                 u=Depends(require_role("super_admin", "admin")),
                                 s: AsyncSession = Depends(get_session)):
    pid = _safe_uuid(profile_id)
    row = (await s.execute(select(RagflowProfile).where(RagflowProfile.id == pid))).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "配置不存在")
    was_enabled = row.enabled
    await s.delete(row)
    await s.commit()
    if was_enabled:
        # 删除生效配置后自动切到最早一条，避免知识源同步突然失联
        first = (await s.execute(select(RagflowProfile).order_by(RagflowProfile.created_at).limit(1))).scalar_one_or_none()
        if first:
            first.enabled = True
        await _sync_ragflow_settings(s)
        await s.commit()
    return {"ok": True}


@router.put("/ragflow-profiles/{profile_id}/enable")
async def enable_ragflow_profile(profile_id: str,
                                 u=Depends(require_role("super_admin", "admin")),
                                 s: AsyncSession = Depends(get_session)):
    """启用指定配置（切换环境），同时禁用其他所有配置。"""
    pid = _safe_uuid(profile_id)
    row = (await s.execute(select(RagflowProfile).where(RagflowProfile.id == pid))).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "配置不存在")
    await s.execute(update(RagflowProfile).values(enabled=False))
    row.enabled = True
    await _sync_ragflow_settings(s)
    await s.commit()
    return {"ok": True}


# ==================== Embedding 向量模型多配置（多环境/多模型切换，只能生效一条） ====================

def _embedding_to_dict(p: EmbeddingProfile, mask: bool = True) -> dict:
    return {
        "id": str(p.id),
        "name": p.name,
        "api_url": p.api_url,
        "api_key": _mask_api_key(p.api_key) if mask else p.api_key,
        "has_key": bool(p.api_key),
        "model": p.model,
        "enabled": p.enabled,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


async def _seed_embedding_profiles(s: AsyncSession) -> None:
    """一次性迁移：embedding_profiles 表为空且旧版单值已配置时，建档并标记生效。"""
    if (await s.execute(select(EmbeddingProfile).limit(1))).scalar_one_or_none() is not None:
        return
    api_url = await _effective_setting(s, "embedding_base_url")
    if not api_url:
        return
    s.add(EmbeddingProfile(
        name="默认·旧配置迁移",
        api_url=api_url.strip(),
        api_key=await _effective_setting(s, "embedding_api_key"),
        model=await _effective_setting(s, "embedding_model"),
        enabled=True,
    ))
    await s.commit()


async def _sync_embedding_settings(s: AsyncSession) -> None:
    """生效配置回写旧版单值 settings，kb_common.rag.embedder（本地 RAG 入库/检索）链路保持兼容。

    无生效配置时清空旧键：本地 RAG 检索/入库提示未配置。
    """
    row = (await s.execute(select(EmbeddingProfile).where(EmbeddingProfile.enabled == True))).scalar_one_or_none()  # noqa: E712
    values = ([("embedding_base_url", row.api_url), ("embedding_api_key", row.api_key), ("embedding_model", row.model)] if row
              else [("embedding_base_url", ""), ("embedding_api_key", ""), ("embedding_model", "")])
    for k, v in values:
        setting = (await s.execute(select(Setting).where(Setting.key == k))).scalar_one_or_none()
        if setting:
            setting.value = v
        else:
            s.add(Setting(key=k, value=v, is_secret=(k == "embedding_api_key")))


class EmbeddingProfileIn(BaseModel):
    name: str
    api_url: str = ""
    api_key: str = ""
    model: str = ""


@router.get("/embedding-profiles")
async def list_embedding_profiles(u=Depends(require_role("super_admin", "admin")),
                                  s: AsyncSession = Depends(get_session)):
    await _seed_embedding_profiles(s)
    rows = (await s.execute(select(EmbeddingProfile).order_by(EmbeddingProfile.created_at))).scalars().all()
    return [_embedding_to_dict(r) for r in rows]


@router.post("/embedding-profiles")
async def create_embedding_profile(body: EmbeddingProfileIn,
                                   u=Depends(require_role("super_admin", "admin")),
                                   s: AsyncSession = Depends(get_session)):
    if not body.name.strip():
        raise HTTPException(400, "配置名称不能为空")
    if not body.api_url.strip():
        raise HTTPException(400, "服务地址不能为空")
    if not body.model.strip():
        raise HTTPException(400, "模型名不能为空")
    profile = EmbeddingProfile(
        name=body.name.strip(),
        api_url=body.api_url.strip(),
        api_key=body.api_key.strip(),
        model=body.model.strip(),
        enabled=False,
    )
    s.add(profile)
    await s.commit()
    await s.refresh(profile)
    return _embedding_to_dict(profile)


@router.put("/embedding-profiles/{profile_id}")
async def update_embedding_profile(profile_id: str, body: EmbeddingProfileIn,
                                   u=Depends(require_role("super_admin", "admin")),
                                   s: AsyncSession = Depends(get_session)):
    pid = _safe_uuid(profile_id)
    row = (await s.execute(select(EmbeddingProfile).where(EmbeddingProfile.id == pid))).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "配置不存在")
    if not body.name.strip():
        raise HTTPException(400, "配置名称不能为空")
    if not body.api_url.strip():
        raise HTTPException(400, "服务地址不能为空")
    if not body.model.strip():
        raise HTTPException(400, "模型名不能为空")
    row.name = body.name.strip()
    row.api_url = body.api_url.strip()
    row.model = body.model.strip()
    # Key 语义：新值非掩码 → 覆盖；留空/掩码 → 保留原值（内网免鉴权服务可无 Key）
    new_key = body.api_key.strip()
    if new_key and "****" not in new_key:
        row.api_key = new_key
    if row.enabled:
        await _sync_embedding_settings(s)
    await s.commit()
    await s.refresh(row)
    return _embedding_to_dict(row)


@router.delete("/embedding-profiles/{profile_id}")
async def delete_embedding_profile(profile_id: str,
                                   u=Depends(require_role("super_admin", "admin")),
                                   s: AsyncSession = Depends(get_session)):
    pid = _safe_uuid(profile_id)
    row = (await s.execute(select(EmbeddingProfile).where(EmbeddingProfile.id == pid))).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "配置不存在")
    was_enabled = row.enabled
    await s.delete(row)
    await s.commit()
    if was_enabled:
        # 删除生效配置后自动切到最早一条，避免本地 RAG 检索突然失联
        first = (await s.execute(select(EmbeddingProfile).order_by(EmbeddingProfile.created_at).limit(1))).scalar_one_or_none()
        if first:
            first.enabled = True
        await _sync_embedding_settings(s)
        await s.commit()
    return {"ok": True}


@router.put("/embedding-profiles/{profile_id}/enable")
async def enable_embedding_profile(profile_id: str,
                                   u=Depends(require_role("super_admin", "admin")),
                                   s: AsyncSession = Depends(get_session)):
    """启用指定配置（切换环境/模型），同时禁用其他所有配置。"""
    pid = _safe_uuid(profile_id)
    row = (await s.execute(select(EmbeddingProfile).where(EmbeddingProfile.id == pid))).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "配置不存在")
    await s.execute(update(EmbeddingProfile).values(enabled=False))
    row.enabled = True
    await _sync_embedding_settings(s)
    await s.commit()
    return {"ok": True}


# ==================== 菜单显示配置（侧边栏功能区菜单显隐） ====================
# 存储：settings 表 key="menu_visibility"，value 为 JSON：{"hidden": ["/chat", ...]}
# 未配置或解析失败 → 空列表（全部菜单默认显示）
_MENU_VISIBILITY_KEY = "menu_visibility"


def _load_menu_visibility(raw: str | None) -> list[str]:
    """解析已保存的隐藏菜单路径列表，异常时回退为空（全部显示）。"""
    if not raw:
        return []
    try:
        data = _json.loads(raw)
        if isinstance(data, dict):
            hidden = data.get("hidden")
        else:
            hidden = data
        if isinstance(hidden, list):
            return [str(x) for x in hidden if x]
    except Exception:
        pass
    return []


@router.get("/menu-visibility")
async def get_menu_visibility(u=Depends(get_principal),
                             s: AsyncSession = Depends(get_session)):
    """菜单显示配置读取（所有登录用户可读，供侧边栏渲染）。"""
    row = (await s.execute(select(Setting).where(Setting.key == _MENU_VISIBILITY_KEY))).scalar_one_or_none()
    return {"hidden": _load_menu_visibility(row.value if row else None)}


class MenuVisibilityIn(BaseModel):
    hidden: list[str] = []


@router.put("/menu-visibility")
async def set_menu_visibility(body: MenuVisibilityIn,
                              u=Depends(require_role("super_admin", "admin")),
                              s: AsyncSession = Depends(get_session)):
    """菜单显示配置保存（管理员可写），整体覆盖式更新。"""
    clean = sorted({str(x).strip() for x in body.hidden if str(x).strip()})
    row = (await s.execute(select(Setting).where(Setting.key == _MENU_VISIBILITY_KEY))).scalar_one_or_none()
    value = _json.dumps({"hidden": clean}, ensure_ascii=False)
    if row:
        row.value = value
    else:
        s.add(Setting(key=_MENU_VISIBILITY_KEY, value=value, is_secret=False))
    await s.commit()
    return {"ok": True, "hidden": clean}


@router.get("/llm-enabled-models")
async def list_enabled_llm_models(u=Depends(get_principal),
                                  s: AsyncSession = Depends(get_session)):
    """智能问答模型下拉：所有 enabled=true 的模型；is_default 为新会话默认。"""
    rows = (await s.execute(select(LLMProfile).order_by(LLMProfile.created_at))).scalars().all()
    out: list[dict] = []
    for p in rows:
        for m in _parse_models(p.models):
            if m.get("enabled"):
                out.append({
                    "profile_id": str(p.id),
                    "profile_name": p.name,
                    "provider": p.provider,
                    "model": m["name"],
                    "is_default": bool(m.get("is_default")),
                })
    return {"models": out}
