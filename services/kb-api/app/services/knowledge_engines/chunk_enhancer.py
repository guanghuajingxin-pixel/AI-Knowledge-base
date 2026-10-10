"""Generate and assemble per-chunk retrieval metadata."""
from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping

from kb_common.clients import llm_client


_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?:images/)?([^\s)]+)(?:\s+[^)]*)?\)", re.IGNORECASE)


def referenced_image_names(content: str) -> list[str]:
    """Return unique image basenames referenced by a chunk."""
    return list(dict.fromkeys(match.group(1).rsplit("/", 1)[-1]
                              for match in _IMAGE_RE.finditer(content)))


def _parse_json(raw: str) -> dict:
    value = raw.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.IGNORECASE)
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("模型未返回 JSON 对象")
    return parsed


async def enhance_chunk(content: str, filename: str, enhancements: Mapping[str, bool],
                        images: Mapping[str, bytes], llm: Mapping[str, str]) -> dict:
    """Return persisted metadata; generation failures are surfaced per chunk, not hidden."""
    result: dict = {"filename": filename if enhancements.get("include_filename") else "",
                    "summary": "", "questions": [], "image_captions": [], "errors": {}}
    want_summary = bool(enhancements.get("auto_summary"))
    want_questions = bool(enhancements.get("auto_questions"))
    image_names = []
    referenced = []
    if enhancements.get("image_caption"):
        image_names = referenced_image_names(content)
        referenced = [name for name in image_names if name in images]
        if image_names and len(referenced) < len(image_names):
            result["errors"]["image_caption"] = "解析产物中未找到全部图片文件"

    if not (want_summary or want_questions or referenced):
        return result
    if not llm.get("base_url") or not llm.get("model") or not llm.get("api_key"):
        message = "未配置可用的大模型，请在模型配置中设置 LLM 地址、模型和密钥"
        if want_summary:
            result["errors"]["summary"] = message
        if want_questions:
            result["errors"]["questions"] = message
        if referenced:
            result["errors"]["image_caption"] = message
        return result

    request_images = referenced[:4]
    tasks = []
    if want_summary:
        tasks.append('"summary": "用 1-3 句话概括分段核心信息"')
    if want_questions:
        tasks.append('"questions": ["生成 2-3 个用户可能提出的具体问题"]')
    if request_images:
        tasks.append('"image_captions": [{"image": "原图片文件名", "caption": "客观描述图片主体和可见信息"}]')
    prompt = (
        "请基于给定分段生成检索增强信息。不要补充原文中没有的事实；用简洁中文；"
        "只输出合法 JSON 对象，字段必须为：" + ", ".join(tasks) +
        "。分段内容如下：\n" + content[:12000]
    )
    body: list[dict] = [{"type": "text", "text": prompt}]
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp", "gif": "image/gif"}
    for name in request_images:
        ext = name.rsplit(".", 1)[-1].lower()
        media = mime.get(ext)
        if not media:
            result["errors"]["image_caption"] = f"不支持生成该图片格式的描述：{ext or '未知'}"
            continue
        encoded = base64.b64encode(images[name]).decode("ascii")
        body.append({"type": "image_url", "image_url": {"url": f"data:{media};base64,{encoded}"}})

    try:
        raw = await llm_client.chat(
            [{"role": "system", "content": "你是严谨的知识检索增强助手。"},
             {"role": "user", "content": body}],
            model=llm["model"], base_url=llm["base_url"], api_key=llm["api_key"])
        generated = _parse_json(raw)
        if want_summary:
            result["summary"] = str(generated.get("summary") or "").strip()[:1200]
            if not result["summary"]:
                result["errors"]["summary"] = "模型未返回摘要"
        if want_questions:
            result["questions"] = [str(q).strip()[:240] for q in generated.get("questions", [])
                                   if str(q).strip()][:5]
            if not result["questions"]:
                result["errors"]["questions"] = "模型未返回候选问题"
        if request_images:
            captions = generated.get("image_captions") or []
            by_name = {str(item.get("image") or "").rsplit("/", 1)[-1]: str(item.get("caption") or "").strip()
                       for item in captions if isinstance(item, dict)}
            result["image_captions"] = [{"image": name, "caption": by_name[name][:600]}
                                        for name in request_images if by_name.get(name)]
            if len(result["image_captions"]) < len(referenced):
                result["errors"].setdefault("image_caption", "模型未能描述全部图片")
    except Exception:  # external model failures must be visible and must not erase parsed text
        if want_summary:
            result["errors"]["summary"] = "模型调用失败，请检查模型配置"
        if want_questions:
            result["errors"]["questions"] = "模型调用失败，请检查模型配置"
        if referenced:
            result["errors"]["image_caption"] = "模型调用失败，请确认当前模型支持图片输入"
    return result


def retrieval_text(content: str, metadata: Mapping | None) -> str:
    """Combine original text and generated fields for lexical and vector recall."""
    metadata = metadata or {}
    fields = [content]
    if metadata.get("filename"):
        fields.append(f"文档名：{metadata['filename']}")
    if metadata.get("summary"):
        fields.append(f"内容摘要：{metadata['summary']}")
    questions = metadata.get("questions") or []
    if questions:
        fields.append("候选问题：" + "；".join(str(q) for q in questions))
    captions = metadata.get("image_captions") or []
    if captions:
        fields.append("图片描述：" + "；".join(str(c.get("caption") or "") for c in captions))
    return "\n\n".join(value for value in fields if value)
