"""DeerFlow 2.0 QA Sidecar —— 知识治理平台智能问答的 Agent 底座。

以 DeerFlow 2.0 super agent harness（LangChain 1.x create_agent + 中间件链 +
子智能体 + 长期记忆 + 上下文摘要压缩）为运行时，通过 SSE 向 kb-api 输出
Agent 执行事件（工具调用、AI 文本、结束）。

模型配置不从本仓库的 config.yaml 静态维护，而是启动时从 kb-api
（/api/v1/agent/bootstrap，内部令牌鉴权）拉取系统配置中心的 LLM 设置并
生成 config.yaml，保证「系统配置」页是唯一配置入口。

启动（见 scripts/start-deerflow.sh）：
    cd services/deerflow/backend
    KB_API_URL=http://127.0.0.1:8000 KB_INTERNAL_TOKEN=... \
        uv run uvicorn app.qa_server:app --host 127.0.0.1 --port 2027
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# 确保 backend 目录在 sys.path 上，使 `extensions.*` 与 `deerflow.*` 可导入
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

DATA_DIR = Path(os.getenv("DEER_FLOW_HOME", str(BACKEND_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = Path(os.getenv("DEER_FLOW_CONFIG_PATH", str(BACKEND_DIR / "config.yaml")))

# 人格（SOUL.md）路径
# - SOUL.md：DeerFlow 运行时实际读取的人格文件（每次引导按自定义或默认模板重写）
# - persona.custom.md：用户在前端保存的自定义内容，重启/重载不丢失；删除即恢复默认
SOUL_PATH = DATA_DIR / "SOUL.md"
PERSONA_CUSTOM_PATH = DATA_DIR / "persona.custom.md"
_DEFAULT_AGENT_NAME = "杰克百晓生"
_BOOT_AGENT_NAME = ""   # 引导/热更新时从 kb-api 配置读取的智能体名
SKILLS_DIR = BACKEND_DIR.parent / "skills"
_MAX_PROMPT_CHARS = 20000

os.environ.setdefault("DEER_FLOW_HOME", str(DATA_DIR))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [deerflow] %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("qa_server")

# ---------------------------------------------------------------------------
# 人格与技能
# ---------------------------------------------------------------------------

SOUL_MD = """你是「杰克百晓生」，知识治理平台内置的智能助手。

你的行为由两部分共同驱动：
- 人格（SOUL.md，即本文档）：定义你的身份、语气与总体行为准则；
- 技能库（SKILL.md）：面对具体任务时，按已启用技能的适用场景与流程执行。

每次对话前你都会获得当前已启用技能的说明。当用户的任务命中某个技能的适用场景时，
严格按该技能定义的条件、步骤与输出规范执行；技能未覆盖的任务按通用助手方式处理。

总体行为准则：
- 中文作答，先结论后细节，要点分条；
- 只做当前被要求的事，不附加未被询问的信息；
- 不向用户展示内部思考、工具参数、检索过程等过程性内容；
- 需要用户补充信息时，用 ask_clarification 澄清。
"""

# ---------------------------------------------------------------------------
# 配置引导：从 kb-api 拉取 LLM 配置，生成 DeerFlow config.yaml
# ---------------------------------------------------------------------------

def _kb_api_url() -> str:
    return os.getenv("KB_API_URL", "http://127.0.0.1:8000").rstrip("/")


def _internal_headers() -> dict[str, str]:
    return {"X-Internal-Token": os.getenv("KB_INTERNAL_TOKEN", "")}


def _bootstrap_from_kbapi(timeout: float = 10.0) -> dict[str, Any] | None:
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(
                f"{_kb_api_url()}/api/v1/agent/bootstrap",
                headers=_internal_headers(),
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("bootstrap from kb-api failed: %s", e)
        return None


def _bootstrap_from_env() -> dict[str, Any] | None:
    api_key = os.getenv("DF_LLM_API_KEY", "").strip()
    if not api_key:
        return None
    return {
        "model": os.getenv("DF_LLM_MODEL", "deepseek-chat"),
        "base_url": os.getenv("DF_LLM_BASE_URL", "https://api.deepseek.com/v1"),
        "api_key": api_key,
        "temperature": float(os.getenv("DF_LLM_TEMPERATURE", "0.7")),
        "max_tokens": int(os.getenv("DF_LLM_MAX_TOKENS", "4096")),
    }


def write_config(boot: dict[str, Any]) -> None:
    """根据引导配置生成 DeerFlow config.yaml。"""
    config = {
        "config_version": 3,
        "models": [
            {
                "name": "kb-default",
                "display_name": "杰克百晓生",
                "use": "langchain_openai:ChatOpenAI",
                "model": boot.get("model") or "deepseek-chat",
                "api_key": boot.get("api_key", ""),
                "base_url": boot.get("base_url") or "https://api.deepseek.com/v1",
                "max_tokens": int(boot.get("max_tokens") or 4096),
                "temperature": float(boot.get("temperature") or 0.7),
                **({"top_p": float(boot["top_p"])} if boot.get("top_p") else {}),
                "supports_thinking": False,
                "supports_vision": False,
            }
        ],
        "tool_groups": [{"name": "kb"}],
        "tools": [
            {
                "name": "knowledge_search",
                "group": "kb",
                "use": "extensions.kb_tools:knowledge_search_tool",
            },
            {
                "name": "dingtalk_search",
                "group": "kb",
                "use": "extensions.kb_tools:dingtalk_search_tool",
            },
            {
                "name": "dingtalk_browse",
                "group": "kb",
                "use": "extensions.kb_tools:dingtalk_browse_tool",
            },
            {
                "name": "dingtalk_read_doc",
                "group": "kb",
                "use": "extensions.kb_tools:dingtalk_read_doc_tool",
            },
            {
                "name": "knowledge_context_expand",
                "group": "kb",
                "use": "extensions.kb_tools:knowledge_context_expand_tool",
            }
        ],
        "tool_search": {"enabled": False},
        "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
        "skills": {"path": "../skills", "container_path": "/mnt/skills"},
        "title": {"enabled": True, "max_words": 6, "max_chars": 60, "model_name": None},
        "summarization": {
            "enabled": True,
            "model_name": None,
            "trigger": [{"type": "tokens", "value": 12000}],
            "keep": {"type": "messages", "value": 10},
            "trim_tokens_to_summarize": 12000,
            "summary_prompt": None,
        },
        "memory": {
            "enabled": True,
            "storage_path": "memory.json",
            "debounce_seconds": 30,
            "model_name": None,
            "max_facts": 100,
            "fact_confidence_threshold": 0.7,
            "injection_enabled": True,
            "max_injection_tokens": 2000,
        },
        "checkpointer": {"type": "sqlite", "connection_string": "checkpoints.db"},
    }
    CONFIG_PATH.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    logger.info("config.yaml written: model=%s base_url=%s", config["models"][0]["model"], config["models"][0]["base_url"])


def _effective_persona() -> str:
    """当前生效的人格内容：优先自定义，否则默认模板；并按配置页 agent_name 替换自称。"""
    if PERSONA_CUSTOM_PATH.exists():
        text = PERSONA_CUSTOM_PATH.read_text(encoding="utf-8")
    else:
        text = SOUL_MD
    name = (_BOOT_AGENT_NAME or "").strip()
    if name and name != _DEFAULT_AGENT_NAME:
        text = text.replace(_DEFAULT_AGENT_NAME, name)
    from app.qa_events import ANSWER_SCOPE
    return text + "\n\n" + ANSWER_SCOPE


_FORCE_RETRIEVAL_PREFIX = (
    "【平台约束·强制检索模式】无论问题类型（含问候/闲聊），本轮必须先调用 knowledge_search "
    "获取企业知识后再作答；钉钉探索必须先获用户选择；检索无结果时明确说明未检索到相关内容，不得跳过检索。\n\n"
)


def _apply_retrieval_mode(message: str, mode: str) -> str:
    """retrieval_mode=force 时注入平台约束前缀；smart 原样返回。"""
    if (mode or "").strip().lower() == "force":
        return _FORCE_RETRIEVAL_PREFIX + message
    return message


def write_persona() -> None:
    """把生效中的人格写入 DeerFlow 运行时读取的文件。"""
    SOUL_PATH.write_text(_effective_persona(), encoding="utf-8")


def ensure_bootstrapped() -> bool:
    """启动引导：拉取配置 → 写 config.yaml + SOUL.md。成功返回 True。"""
    global _BOOT_AGENT_NAME
    boot = _bootstrap_from_kbapi() or _bootstrap_from_env()
    if not boot or not boot.get("api_key"):
        logger.error("no LLM bootstrap config available (kb-api unreachable and DF_LLM_API_KEY not set)")
        return False
    _BOOT_AGENT_NAME = str(boot.get("agent_name") or "").strip()
    write_config(boot)
    write_persona()
    logger.info("persona ready (persona_custom=%s)", PERSONA_CUSTOM_PATH.exists())
    return True


# ---------------------------------------------------------------------------
# Agent 运行时
# ---------------------------------------------------------------------------

_client = None
_client_lock = threading.Lock()
_ensure_lock = threading.Lock()  # _ensure_agent 构建互斥（agent 为 client 级单例）


class _NullLock:
    """并发化占位：原「整条流一把全局锁」已下沉为 checkpointer 方法级互斥
    （见 _serialize_checkpointer）——多会话的 LLM 流式长任务可并行执行，
    仅 sqlite 检查点短 IO 在共享连接上串行。保留 with _stream_lock 结构
    避免大段代码重排，语义从「全局串行」变为「无操作」。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_stream_lock = _NullLock()


def get_client():
    """惰性创建 DeerFlowClient（配置就绪后）。"""
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            from deerflow.client import DeerFlowClient  # noqa: PLC0415

            os.environ["DEER_FLOW_CONFIG_PATH"] = str(CONFIG_PATH)
            _client = DeerFlowClient(
                config_path=str(CONFIG_PATH),
                thinking_enabled=False,
                subagent_enabled=False,
                plan_mode=False,
            )
            logger.info("DeerFlowClient initialized")
    return _client


def reset_client() -> None:
    global _client
    with _client_lock:
        if _client is not None:
            _client.reset_agent()
    _client = None


class ChatStreamIn(BaseModel):
    thread_id: str
    message: str
    thinking_enabled: bool = False
    plan_mode: bool = False
    subagent_enabled: bool = False
    recursion_limit: int = 80
    dataset_ids: list[str] | None = None
    ragflow_dataset_ids: list[str] | None = None  # RAGFlow 数据集（与 Dify 并列的第二检索引擎）
    kb_ids: list[str] | None = None    # 平台本地知识库（ES hybrid）ID 列表
    doc_library_ids: list[int] | None = None  # 平台文档库（knowledge_libraries）ID 列表，本地检索通道
    top_k: int = 8
    disabled_tools: list[str] = []   # 停用的工具名（来自智能体配置 tools_enabled）
    retrieval_mode: str = "smart"    # smart=智能调用；force=每问必检索（平台约束前缀注入）
    max_retrieval_rounds: int = 2    # 单轮 knowledge_search 调用上限=轮数×3（kb_tools 硬限）
    # 限时探索续跑动作：""=新问题（重置计时）；"continue"=用户选择继续探索（进入下一计时阶段）；
    # "stop"=用户选择先基于已检索内容回答（立即停止检索）
    action: str = ""


app = FastAPI(title="DeerFlow QA Sidecar", version="2.0")


@app.get("/health")
def health():
    ready = CONFIG_PATH.exists()
    return {"status": "ok" if ready else "bootstrapping", "config_ready": ready}


@app.post("/v1/reload")
def reload_config():
    boot = _bootstrap_from_kbapi() or _bootstrap_from_env()
    if not boot or not boot.get("api_key"):
        return {"ok": False, "error": "bootstrap unavailable"}
    write_config(boot)
    write_persona()
    reset_client()
    return {"ok": True, "model": boot.get("model")}


class PersonaIn(BaseModel):
    content: str | None = None
    reset: bool = False


@app.get("/v1/persona")
def get_persona():
    """读取当前生效的人格（SOUL）内容。"""
    return {
        "content": _effective_persona(),
        "custom": PERSONA_CUSTOM_PATH.exists(),
        "default": SOUL_MD,
    }


@app.post("/v1/persona")
def update_persona(body: PersonaIn):
    """保存自定义人格（reset=true 恢复默认）。热重载 Agent，新对话即时生效。"""
    if body.reset:
        PERSONA_CUSTOM_PATH.unlink(missing_ok=True)
    else:
        content = (body.content or "").strip()
        if not content:
            return {"ok": False, "error": "content 不能为空"}
        if len(content) > _MAX_PROMPT_CHARS:
            return {"ok": False, "error": f"内容过长（{len(content)} > {_MAX_PROMPT_CHARS}）"}
        PERSONA_CUSTOM_PATH.write_text(content + "\n", encoding="utf-8")
    SOUL_PATH.write_text(_effective_persona(), encoding="utf-8")
    reset_client()
    logger.info("persona updated (custom=%s)", PERSONA_CUSTOM_PATH.exists())
    return {"ok": True, "custom": PERSONA_CUSTOM_PATH.exists()}


@app.post("/v1/reconfig")
def reconfig():
    """热更新引导配置：kb-api 智能体配置保存后调用。

    重新拉取 bootstrap（模型/超参/agent_name）→ 重写 config.yaml 与 SOUL →
    重置 Agent 单例，新对话即时生效；拉取失败时保持现配置不变。
    """
    global _BOOT_AGENT_NAME
    boot = _bootstrap_from_kbapi()
    if not boot or not boot.get("api_key"):
        return {"ok": False, "error": "bootstrap config unavailable"}
    _BOOT_AGENT_NAME = str(boot.get("agent_name") or "").strip()
    write_config(boot)
    write_persona()
    reset_client()
    logger.info("reconfig applied: model=%s agent_name=%s",
                boot.get("model"), _BOOT_AGENT_NAME or "(default)")
    return {"ok": True}


# ---------------------------------------------------------------------------
# 技能库管理：ClawHub/Agent Skills 通用结构（每个技能一个目录 + SKILL.md）
# 技能目录位于 skills/custom/<slug>/SKILL.md，DeerFlow 在新对话构建提示词时
# 通过 deerflow.skills.loader 扫描加载，新增/删除文件即对新对话生效。
# 全部技能（含企业知识库问答）统一在此管理，不做内置特判。
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9\-_]+")


def _slugify(name: str) -> str:
    """技能目录名：小写字母/数字/中划线/下划线。"""
    slug = _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")
    return slug or f"skill-{int(time.time())}"


def _parse_frontmatter(content: str) -> dict[str, str]:
    """解析 SKILL.md 顶部 YAML frontmatter（简单 key: value）。"""
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


def _sync_frontmatter(content: str, name: str, description: str) -> str:
    """确保 SKILL.md 顶部 frontmatter 含 name/description 且与表单值一致。

    - 无 frontmatter：自动补齐 ClawHub 通用结构；
    - 有 frontmatter：原地更新 name/description（缺失则补充），保留其余字段（如 version）。
    """
    desc = (description or "").strip() or name
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", content, re.DOTALL)
    if not m:
        body = content.strip()
        front = f"---\nname: {name}\ndescription: {desc}\n---\n"
        return front + (f"\n{body}\n" if body else "")

    lines = m.group(1).split("\n")
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and ":" in stripped and not stripped.startswith("#"):
            k = stripped.split(":", 1)[0].strip()
            if k == "name":
                out.append(f"name: {name}")
                seen.add("name")
                continue
            if k == "description":
                out.append(f"description: {desc}")
                seen.add("description")
                continue
        out.append(line)
    if "name" not in seen:
        out.insert(0, f"name: {name}")
    if "description" not in seen:
        out.insert(1 if "name" not in seen else len(out), f"description: {desc}")
    rest = content[m.end():].lstrip("\n")
    return f"---\n" + "\n".join(out) + f"\n---\n\n{rest}".rstrip() + "\n"


def _skill_dir(slug: str, enabled: bool) -> Path:
    """启用技能在 skills/custom/（loader 会扫描）；停用技能移到 skills/disabled/ 保留内容。"""
    root = SKILLS_DIR / ("custom" if enabled else "disabled")
    return root / slug


def _find_skill_dir(slug: str) -> Path | None:
    for enabled in (True, False):
        d = _skill_dir(slug, enabled)
        if d.exists():
            return d
    return None


def _list_custom_skills() -> list[dict[str, Any]]:
    skills: list[dict[str, Any]] = []
    for enabled in (True, False):
        base = SKILLS_DIR / ("custom" if enabled else "disabled")
        if not base.exists():
            continue
        for skill_dir in sorted(base.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            content = skill_file.read_text(encoding="utf-8")
            meta = _parse_frontmatter(content)
            skills.append({
                "name": meta.get("name") or skill_dir.name,
                "slug": skill_dir.name,
                "description": meta.get("description", ""),
                "content": content,
                "enabled": enabled,
            })
    # 启用在前，按名称排序
    skills.sort(key=lambda s: (not s["enabled"], s["name"]))
    return skills


@app.get("/v1/skills")
def list_skills():
    """列出技能（custom=启用，disabled=停用保留内容）。"""
    return {"skills": _list_custom_skills()}


class SkillDocIn(BaseModel):
    name: str
    description: str = ""
    content: str = ""       # 完整 SKILL.md（可不含 frontmatter，服务端自动补齐）
    enabled: bool = True    # False 时写入 disabled 目录，loader 不加载但内容保留
    slug: str = ""          # 编辑场景的旧 slug（改名后用于清除旧目录）


@app.put("/v1/skills")
def put_skill(body: SkillDocIn):
    """新建/覆盖一个技能：写入 skills/custom 或 skills/disabled，新对话即时生效。"""
    import shutil

    name = (body.name or "").strip()
    if not name:
        return {"ok": False, "error": "技能名称不能为空"}
    slug = _slugify(name)

    content = (body.content or "").strip()
    # 始终以表单名称/描述为准同步 frontmatter（ClawHub 结构 name 必填）
    content = _sync_frontmatter(content, name, (body.description or "").strip())

    # 改名场景：旧 slug 与新 slug 不一致时清除旧目录
    old_slug = _slugify(body.slug) if body.slug else ""
    if old_slug and old_slug != slug:
        old_named = _find_skill_dir(old_slug)
        if old_named is not None and SKILLS_DIR.resolve() in old_named.resolve().parents:
            shutil.rmtree(old_named)

    # 先清除另一侧旧目录（启停切换）
    old = _find_skill_dir(slug)
    target = _skill_dir(slug, body.enabled)
    if old is not None and old.resolve() != target.resolve():
        shutil.rmtree(old)
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(content.strip() + "\n", encoding="utf-8")
    logger.info("skill saved: %s (enabled=%s)", slug, body.enabled)
    return {"ok": True, "name": name, "slug": slug, "enabled": body.enabled}


@app.delete("/v1/skills/{slug}")
def delete_skill(slug: str):
    """删除技能目录。"""
    import shutil

    skill_dir = _find_skill_dir(slug)
    if skill_dir is None:
        return {"ok": False, "error": "技能不存在"}
    # 防目录穿越
    if SKILLS_DIR.resolve() not in skill_dir.resolve().parents:
        return {"ok": False, "error": "非法路径"}
    shutil.rmtree(skill_dir)
    logger.info("skill deleted: %s", slug)
    return {"ok": True}


def _sse(obj: dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _serialize_checkpointer(agent) -> None:
    """给共享 checkpointer 的方法加互斥锁（幂等，agent 重建后需再次调用）。

    agent 是 client 级单例，checkpointer（SqliteSaver）持有单个 sqlite 连接，
    非线程安全。方法级锁让多个会话的图并行执行 LLM 流式调用（长任务），
    仅在检查点读写（毫秒级）上互斥，支撑 5+ 会话并发问答。
    """
    cp = getattr(agent, "checkpointer", None)
    if cp is None or getattr(cp, "_kge_method_lock", None):
        return
    # RLock 可重入：SqliteSaver 内部存在方法嵌套（如 get_tuple 首次调用会触发 self.setup()），
    # 同线程重入必须放行，否则非重入 Lock 会自死锁
    lock = threading.RLock()
    for name in ("setup", "get_tuple", "list", "put", "put_writes", "delete_thread"):
        fn = getattr(cp, name, None)
        if fn is None or not callable(fn):
            continue

        def _wrap(f=fn):
            def inner(*args, **kwargs):
                with lock:
                    return f(*args, **kwargs)

            inner.__name__ = getattr(f, "__name__", name)
            return inner

        setattr(cp, name, _wrap())
    cp._kge_method_lock = lock


def _event_stream(body: ChatStreamIn):
    """按根图节点来源转译过程；图结束后只发送本轮主模型的权威答案。

    messages 仅用于来源确认后的阶段提示，绝不转发其 token 内容。
    values 用于历史隔离及最终状态，updates 用于主模型来源和工具调用。
    """
    from extensions.kb_tools import RUNTIME_CTX  # noqa: PLC0415
    from deerflow.agents.middlewares.exploration_timeout_middleware import (
        ExplorationTimeoutMiddleware,
    )
    from deerflow.client import DeerFlowClient  # noqa: PLC0415

    if body.action and not ExplorationTimeoutMiddleware.can_resume(body.thread_id):
        yield _sse({"type": "error", "message": "本次选择已过期或服务已重启，请重新提交原问题。"})
        return

    RUNTIME_CTX[body.thread_id] = {
        "dataset_ids": body.dataset_ids or [],
        "ragflow_dataset_ids": body.ragflow_dataset_ids or [],
        "kb_ids": body.kb_ids or [],
        "doc_library_ids": body.doc_library_ids or [],
        "top_k": body.top_k,
        "retrieval_mode": (body.retrieval_mode or "smart").strip().lower(),
        "max_retrieval_rounds": max(1, int(body.max_retrieval_rounds or 2)),
        "search_calls": 0,
    }
    # 限时探索：新问题重置计时；continue/stop 为用户在澄清按钮上的选择，驱动阶段流转
    ExplorationTimeoutMiddleware.begin(body.thread_id, (body.action or "").strip().lower(), question=body.message)
    if "knowledge_search" in (body.disabled_tools or []):
        ExplorationTimeoutMiddleware.record_evidence(body.thread_id, [])
    try:
        client = get_client()

        # 准备 agent 与 config（复用 client 内部逻辑）
        config = client._get_runnable_config(
            body.thread_id,
            thinking_enabled=body.thinking_enabled,
            plan_mode=body.plan_mode,
            subagent_enabled=body.subagent_enabled,
            recursion_limit=body.recursion_limit,
            # Source selection is mandatory even when optional ambiguity questions are disabled.
            disabled_tools=[t for t in (body.disabled_tools or []) if t != "ask_clarification"],
        )
        # agent 为单例：构建互斥 + 即刻捕获引用；并给共享 checkpointer 上方法级锁（支撑多会话并发）
        with _ensure_lock:
            client._ensure_agent(config)
            agent = client._agent
        _serialize_checkpointer(agent)

        from app.qa_events import AnswerProjection, current_question
        projection = AnswerProjection(body.thread_id)
        state = {"messages": [{"role": "user", "content": _apply_retrieval_mode(
            current_question(body.message, body.action), body.retrieval_mode)}]}
        context = {"thread_id": body.thread_id}
        yield _sse(projection.event("ready"))
        with _stream_lock:
            for mode, chunk in agent.stream(
                state, config=config, context=context, stream_mode=["values", "updates", "messages"],
            ):
                if ExplorationTimeoutMiddleware.is_cancelled(body.thread_id):
                    yield _sse(projection.event("cancelled"))
                    return
                for event in projection.feed(mode, chunk):
                    if event["type"] == "tool_start" and event.get("name", "").startswith("dingtalk_"):
                        if ExplorationTimeoutMiddleware.dingtalk_budget(body.thread_id) <= 0:
                            continue  # Proposed by an old prompt, blocked before execution.
                    if event["type"] == "tool_end":
                        event["content"] = DeerFlowClient._extract_text(event["content"])
                    yield _sse(event)
            if ExplorationTimeoutMiddleware.is_cancelled(body.thread_id):
                yield _sse(projection.event("cancelled"))
                return
            final = projection.finish(DeerFlowClient._extract_text)
            if final is not None:
                yield _sse(final)
            yield _sse(projection.event("end", usage=projection.usage))
    except Exception as e:  # noqa: BLE001
        logger.exception("agent stream failed")
        yield _sse({"type": "error", "message": f"{e.__class__.__name__}: {e}"})
    finally:
        RUNTIME_CTX.pop(body.thread_id, None)


@app.post("/v1/chat/stream")
async def chat_stream(request: Request, body: ChatStreamIn):
    if not CONFIG_PATH.exists():
        async def _not_ready():
            yield _sse({"type": "error", "message": "deerflow not bootstrapped: LLM config missing"})
        return StreamingResponse(_not_ready(), media_type="text/event-stream")

    import anyio

    gen = _event_stream(body)  # 同步生成器（内部 agent.stream）
    tid = body.thread_id
    from deerflow.agents.middlewares.exploration_timeout_middleware import (
        ExplorationTimeoutMiddleware,
    )

    if body.action and not ExplorationTimeoutMiddleware.can_resume(tid):
        async def _expired_choice():
            yield _sse({"type": "error", "message": "本次选择已过期或服务已重启，请重新提交原问题。"})
        return StreamingResponse(_expired_choice(), media_type="text/event-stream")

    async def _pump():
        """用一个专属 worker 线程驱动同步生成器，经 asyncio.Queue 转发为 SSE。

        为什么不用 run_sync 逐帧在线程池里 next()：同步生成器若被 starlette
        线程池迭代，客户端断连会被「遗弃」——既不 close 也不释放 _stream_lock，
        且底层图可能空跑耗 token。这里：
          - gen 全程只被一个专属线程迭代（无线程交叉）；
          - 每帧检查「用户取消标志」或「客户端断连」，命中即置 stop_flag、发
            cancelled 帧给 kb-api；
          - worker 停止 next() 后，在【同一驱动线程】内 gen.close()（GeneratorExit
            传进 agent.stream，中断进行中的模型流式调用、释放 _stream_lock）；
          - finally 用 shield 等 worker 收尾，保证断连路径也能干净停止。
        """
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        done_sentinel = object()
        cancel_sentinel = object()
        stop_flag = threading.Event()

        def _worker():
            try:
                while not stop_flag.is_set():
                    try:
                        item = next(gen)
                    except StopIteration:
                        break  # 生成器正常结束（或 _event_stream 检测到取消后自行 return）
                    except Exception:  # noqa: BLE001
                        logger.exception("[qa] stream worker iteration failed")
                        break
                    try:
                        loop.call_soon_threadsafe(q.put_nowait, item)
                    except RuntimeError:
                        return  # 事件循环已关闭
                    if stop_flag.is_set():
                        break  # gen 挂起在产出该帧的 yield 处，finally 里 close
            finally:
                # gen 只在本线程迭代：此时它要么已结束、要么挂起在最后一次 yield。
                # 在此 close() 向它注入 GeneratorExit，确定性拆图（中断模型流式调用、
                # 释放 _stream_lock、pop RUNTIME_CTX）。
                try:
                    gen.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    # 若因用户中断而结束，补一个取消标记，保证 kb-api 确定性收到 cancelled 帧
                    # （取消可能发生在 pump 等待下一帧期间、没有新帧可触发判断）。
                    if ExplorationTimeoutMiddleware.is_cancelled(tid):
                        loop.call_soon_threadsafe(q.put_nowait, cancel_sentinel)
                    loop.call_soon_threadsafe(q.put_nowait, done_sentinel)
                except RuntimeError:
                    pass

        worker = threading.Thread(target=_worker, name=f"qa-stream-{tid[-8:]}", daemon=True)
        worker.start()

        sending = True
        terminal_sent = False

        def _begin_stop() -> bool:
            """标记取消并通知 worker 停止；仅首次（仍在发送时）返回 True 由调用方下发 cancelled 帧。"""
            nonlocal sending
            ExplorationTimeoutMiddleware.request_cancel(tid)
            stop_flag.set()
            if sending:
                sending = False
                return True
            return False

        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    # 等待下一帧期间也主动检测取消/断连（取消信号不依赖新帧到达）
                    if sending and (
                        ExplorationTimeoutMiddleware.is_cancelled(tid) or await request.is_disconnected()
                    ):
                        logger.info("[qa] thread=%s stop detected while waiting, closing graph", tid)
                        if _begin_stop():
                            try:
                                yield _sse({"type": "cancelled"})
                            except Exception:  # noqa: BLE001
                                pass
                    continue
                if item is done_sentinel:
                    # 流结束：若已取消但还没下发 cancelled 帧（worker 因取消自终止），补发
                    if sending and ExplorationTimeoutMiddleware.is_cancelled(tid):
                        _begin_stop()
                        try:
                            yield _sse({"type": "cancelled"})
                        except Exception:  # noqa: BLE001
                            pass
                    return
                if item is cancel_sentinel:
                    logger.info("[qa] thread=%s worker ended by cancel, signalling kb-api", tid)
                    if _begin_stop():
                        try:
                            yield _sse({"type": "cancelled"})
                        except Exception:  # noqa: BLE001
                            pass
                    continue
                if sending:
                    if ExplorationTimeoutMiddleware.is_cancelled(tid) or await request.is_disconnected():
                        # 用户点停止或客户端断连：置取消标志（中间件在节点边界短路未开始的
                        # 模型调用），通知 worker 停止拉取并 close 图。
                        logger.info("[qa] thread=%s stop requested, closing graph", tid)
                        if _begin_stop():
                            try:
                                yield _sse({"type": "cancelled"})
                            except Exception:  # noqa: BLE001
                                pass
                        continue  # 继续抽空队列直到 done_sentinel，不再下发
                    try:
                        if json.loads(item.removeprefix("data: ").strip()).get("type") == "end":
                            terminal_sent = True
                    except (ValueError, AttributeError):
                        pass
                    yield item
                # sending=False 时继续消费队列直到 done_sentinel
        finally:
            stop_flag.set()
            if not terminal_sent:
                ExplorationTimeoutMiddleware.request_cancel(tid)
            # shield：即使本协程因断连被取消，也要等 worker 把 gen close 干净
            # （释放 _stream_lock），避免后续请求卡锁。
            with anyio.CancelScope(shield=True):
                try:
                    await anyio.to_thread.run_sync(lambda: worker.join(timeout=60))
                except Exception:  # noqa: BLE001
                    logger.exception("[qa] stream worker join failed")

    return StreamingResponse(_pump(), media_type="text/event-stream")


class ChatCancelIn(BaseModel):
    thread_id: str


@app.post("/v1/chat/cancel")
def chat_cancel(body: ChatCancelIn):
    """用户手动中断：标记 thread 取消，进行中的 SSE 流在下一个事件边界停止，
    未开始的模型调用由 ExplorationTimeoutMiddleware 短路（不再消耗 token）。"""
    from deerflow.agents.middlewares.exploration_timeout_middleware import ExplorationTimeoutMiddleware
    active = ExplorationTimeoutMiddleware.request_cancel(body.thread_id)
    logger.info("[qa] cancel request thread=%s active=%s", body.thread_id, active)
    return {"cancelled": bool(active)}


def _bootstrap_with_retry(max_tries: int = 40, interval: float = 5.0) -> None:
    """kb-api 可能晚于本服务启动：后台重试引导，直到成功或超时。"""
    for i in range(max_tries):
        if ensure_bootstrapped():
            logger.info("bootstrap ok after %d tries, sidecar ready", i + 1)
            return
        time.sleep(interval)
    logger.error("bootstrap still failing after %d tries; call POST /v1/reload manually", max_tries)


# 启动时尝试引导（失败不阻塞进程：后台重试，/v1/reload 也可手动补偿）
if ensure_bootstrapped():
    logger.info("bootstrap ok, sidecar ready")
else:
    logger.warning("bootstrap skipped at startup; retrying in background...")
    threading.Thread(target=_bootstrap_with_retry, daemon=True).start()
