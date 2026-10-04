"""杰克百晓生智能体配置 —— 以 JSON 存储于 settings 表（key=agent_config）。

参考 DeerFlow（规划 → 检索 → 反思 → 报告）与平台智能体配置项，
将知识库调用策略、任务规划、长期记忆、追问建议、生成超参等全部配置化。
"""
from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kb_common.models import Setting

CONFIG_KEY = "agent_config"

# 智能问答可绑定工具目录（key 与 DeerFlow sidecar 工具注册名一致）
TOOL_CATALOG: list[dict[str, str]] = [
    {"key": "knowledge_search", "name": "知识库检索",
     "desc": "检索企业知识库（统一知识库），答案的主要来源"},
    {"key": "dingtalk_search", "name": "钉钉知识库检索",
     "desc": "知识库证据不足时，通过 DWS 实时检索已授权的企业文档内容"},
    {"key": "dingtalk_read_doc", "name": "钉钉文档读取",
     "desc": "读取在线 adoc 文档原文；办公文件使用搜索返回的正文片段"},
    {"key": "ask_clarification", "name": "澄清提问",
     "desc": "用户问题模糊时主动反问，确认意图后再作答"},
]

DEFAULT_CONFIG: dict[str, Any] = {
    # —— 基础设置 ——
    "agent_name": "杰克百晓生",
    "bot_avatar": "",             # 机器人头像（data: 图片或 http(s) 链接；空=默认图标）
    "models": [],                 # 参与调度的模型名列表（最多 10）；为空则用系统配置的 llm_model
    "default_model": "",          # 智能体默认模型（须为 models 之一）；为空跟随列表首个模型
    "retrieval_mode": "smart",    # smart=智能调用（闲聊/问候不检索）；force=强制调用（每问必检索）
    # 问答检索的知识库范围（knowledge_libraries 表 ID）：空=不检索（未选择=不使用）；
    # 文档库走本地检索通道，DIFY/RAGFlow 外部库走各自引擎通道
    "knowledge_library_ids": [],
    # 智能体级停用的选库（knowledge_library_ids 的子集，UI 开关控制）：
    # 停用的库不参与该智能体检索（不删除绑定，可随时重新启用），随配置一起保存
    "knowledge_library_disabled_ids": [],
    "greeting_enabled": True,     # 对话开场白
    "greeting": "你好！我是杰克百晓生，公司知识问答助手。",
    "suggested_questions": [      # 开场白下的推荐问题
        "安全标识管理制度包含哪些内容？",
        "设备保养流程怎么走？",
        "差旅报销需要哪些审批角色？",
    ],
    "follow_up_enabled": True,    # 下一步问题建议（完整回答后提供原文追问）
    # —— 工具开关（key=DeerFlow 工具名；False=该工具不绑定给智能体）——
    "tools_enabled": {
        "knowledge_search": True,   # 知识库检索（统一知识库，答案主要来源）
        "dingtalk_search": True,    # 钉钉知识库检索
        "dingtalk_read_doc": True,  # 钉钉文档正文读取
        "ask_clarification": True,  # 澄清提问（问题模糊时主动反问）
    },
    # —— 高级设置 ——
    "planning_enabled": True,     # 任务规划（TodoList 中间件，复杂问题先拆解计划）
    "subagent_enabled": False,    # 子智能体协作（Lead Agent 按需并行派发 Sub-Agent 调研）
    "long_memory_enabled": True,  # 长期记忆（当前会话最近八条消息，仅用于消解指代）
    # —— 超参维护 ——
    "temperature": 0,           # 生成温度
    "top_p": 0.9,                 # top-p 采样
    "max_tokens": 4096,           # 单次回答最大 token
    "top_k": 8,                   # 知识库召回条数
    "max_retrieval_rounds": 2,    # 检索反思轮数（不充分时改写查询重检，DeerFlow Researcher 反思循环）
    # —— 外部平台智能体（嵌入网页接入；切换智能体仅需更换嵌入代码）——
    "external_agents": {
        "hiagent": {"enabled": False, "embed_code": "", "url": ""},
        "dify": {"enabled": False, "embed_code": "", "url": ""},
        "deap": {"enabled": False, "embed_code": "", "url": ""},
    },
}


def resolve_agent_model(agent_cfg: dict[str, Any], fallback: str) -> str:
    """智能体生效模型：default_model（管理员选定）→ models 首个 → 系统模型。"""
    models = [str(m).strip() for m in (agent_cfg.get("models") or []) if str(m).strip()]
    dm = str(agent_cfg.get("default_model") or "").strip()
    if dm and (not models or dm in models):
        return dm
    return models[0] if models else fallback


# 外部智能体平台白名单（key 与前端页签一一对应）
EXTERNAL_PLATFORMS = ("hiagent", "dify", "deap")

_IFRAME_SRC_RE = re.compile(r'src\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)


def _normalize_external(raw: Any) -> dict[str, dict[str, Any]]:
    """外部智能体配置白名单：仅保留平台白名单内的字段，嵌入代码自动解析页面地址。"""
    out: dict[str, dict[str, Any]] = {
        p: {"enabled": False, "embed_code": "", "url": ""} for p in EXTERNAL_PLATFORMS
    }
    if not isinstance(raw, dict):
        return out
    for p in EXTERNAL_PLATFORMS:
        item = raw.get(p)
        if not isinstance(item, dict):
            continue
        code = str(item.get("embed_code") or "").strip()[:6000]
        url = str(item.get("url") or "").strip()[:1000]
        # 嵌入代码优先：粘贴 iframe 片段时从中提取 src 作为页面地址；直接粘贴 URL 亦可
        if code:
            m = _IFRAME_SRC_RE.search(code)
            if m:
                url = m.group(1).strip()
            elif re.match(r"^https?://", code.strip()):
                url = code.strip()
                code = ""
        if not re.match(r"^https?://", url):
            url = ""
        out[p] = {"enabled": bool(item.get("enabled")) and bool(url), "embed_code": code, "url": url}
    return out

# 数值字段的合法区间（保存时钳制）
_NUM_BOUNDS = {
    "temperature": (0.0, 2.0),
    "top_p": (0.1, 1.0),
    "max_tokens": (3000, 8192),
    "top_k": (1, 20),
    "max_retrieval_rounds": (1, 2),
}


def _clamp(key: str, val: Any) -> Any:
    if key not in _NUM_BOUNDS:
        return val
    try:
        lo, hi = _NUM_BOUNDS[key]
        v = float(val)
        return max(lo, min(hi, v))
    except (TypeError, ValueError):
        return DEFAULT_CONFIG[key]


async def load_agent_config(session: AsyncSession) -> dict[str, Any]:
    """读取智能体配置；无记录或解析失败时返回默认值。"""
    row = (await session.execute(select(Setting).where(Setting.key == CONFIG_KEY))).scalar_one_or_none()
    if not row or not row.value:
        return dict(DEFAULT_CONFIG)
    try:
        data = json.loads(row.value)
        if not isinstance(data, dict):
            return dict(DEFAULT_CONFIG)
    except (json.JSONDecodeError, TypeError):
        return dict(DEFAULT_CONFIG)
    out = dict(DEFAULT_CONFIG)
    for k in DEFAULT_CONFIG:
        if k in data:
            out[k] = _clamp(k, data[k])
    # 枚举/列表兜底
    if out.get("retrieval_mode") not in ("smart", "force"):
        out["retrieval_mode"] = "smart"
    out["models"] = [str(m).strip() for m in (out.get("models") or []) if str(m).strip()][:10]
    out["default_model"] = str(out.get("default_model") or "").strip()
    if out["default_model"] and out["models"] and out["default_model"] not in out["models"]:
        out["default_model"] = ""
    out["suggested_questions"] = [str(q).strip() for q in (out.get("suggested_questions") or []) if str(q).strip()][:6]
    out["tools_enabled"] = _normalize_tools(data.get("tools_enabled"))
    out["knowledge_library_ids"] = _normalize_library_ids(data.get("knowledge_library_ids"))
    out["knowledge_library_disabled_ids"] = _normalize_library_ids(data.get("knowledge_library_disabled_ids"))
    out["external_agents"] = _normalize_external(data.get("external_agents"))
    out["bot_avatar"] = _normalize_avatar(data.get("bot_avatar"))
    return out


def _normalize_tools(raw: Any) -> dict[str, bool]:
    """工具开关白名单：未知 key 忽略，缺失 key 默认启用。"""
    base = {t["key"]: True for t in TOOL_CATALOG}
    if isinstance(raw, dict):
        for k in base:
            if k in raw:
                base[k] = bool(raw[k])
    return base


def _normalize_library_ids(raw: Any) -> list[int]:
    """智能体检索选库白名单：仅接受正整数 ID 列表（去重、最多 24 个）。"""
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[int] = []
    for v in raw:
        try:
            i = int(v)
        except (TypeError, ValueError):
            continue
        if i > 0 and i not in out:
            out.append(i)
    return out[:24]


def _normalize_avatar(raw: Any) -> str:
    """机器人头像：仅接受 data:image/ 内联图片或 http(s) 链接，最长 512KB，其余置空。"""
    v = str(raw or "").strip()
    if v.startswith(("data:image/", "http://", "https://")) and len(v) <= 512_000:
        return v
    return ""


async def save_agent_config(session: AsyncSession, cfg: dict[str, Any]) -> dict[str, Any]:
    """保存（白名单字段 + 钳制），返回落库后的完整配置。"""
    data = dict(DEFAULT_CONFIG)
    for k in DEFAULT_CONFIG:
        if k in cfg:
            data[k] = _clamp(k, cfg[k])
    if data.get("retrieval_mode") not in ("smart", "force"):
        data["retrieval_mode"] = "smart"
    data["models"] = [str(m).strip() for m in (data.get("models") or []) if str(m).strip()][:10]
    data["default_model"] = str(data.get("default_model") or "").strip()
    if data["default_model"] and data["models"] and data["default_model"] not in data["models"]:
        data["default_model"] = ""
    data["suggested_questions"] = [str(q).strip() for q in (data.get("suggested_questions") or []) if str(q).strip()][:6]
    data["tools_enabled"] = _normalize_tools(cfg.get("tools_enabled"))
    data["knowledge_library_ids"] = _normalize_library_ids(cfg.get("knowledge_library_ids"))
    data["knowledge_library_disabled_ids"] = _normalize_library_ids(cfg.get("knowledge_library_disabled_ids"))
    data["external_agents"] = _normalize_external(cfg.get("external_agents"))
    data["bot_avatar"] = _normalize_avatar(data.get("bot_avatar"))
    for bool_key in ("greeting_enabled", "follow_up_enabled", "planning_enabled",
                     "subagent_enabled", "long_memory_enabled"):
        data[bool_key] = bool(data.get(bool_key))
    data["agent_name"] = str(data.get("agent_name") or DEFAULT_CONFIG["agent_name"]).strip()[:20]
    data["greeting"] = str(data.get("greeting") or "").strip()[:1000]

    row = (await session.execute(select(Setting).where(Setting.key == CONFIG_KEY))).scalar_one_or_none()
    payload = json.dumps(data, ensure_ascii=False)
    if row:
        row.value = payload
    else:
        session.add(Setting(key=CONFIG_KEY, value=payload, is_secret=False))
    await session.commit()
    return data
