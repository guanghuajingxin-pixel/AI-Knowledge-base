"""目录预演、下载与单文件采集共用的源文件类型规则。"""
from pathlib import Path

from kb_common.clients.document_upload import UPLOAD_EXTENSIONS, supported_dify_extensions
from .export_service import ONLINE_TYPES, safe_name

# 本地文档库（document-libraries）支持的扩展名，与 managed_library.upload 校验一致
LIBRARY_EXTENSIONS = {"pdf", "docx", "txt", "md", "csv", "xlsx", "pptx", "html"}


def node_extension(node: dict) -> str:
    return str(node.get("extension") or Path(node.get("name") or "").suffix).lower().lstrip(".")


def online_type(node: dict) -> str:
    ext = node_extension(node)
    if ext in ONLINE_TYPES:
        return ext
    # ALIDOC 也可能带有明确的普通文件后缀，不能把 PDF/Word 导出为 docx。
    if not ext and node.get("category") == "ALIDOC":
        return "adoc"
    return ""


def source_file_name(name: str, ext: str = "", download_name: str = "") -> str:
    base = safe_name(name or download_name)
    if Path(base).suffix and (not ext or Path(base).suffix.lower().lstrip(".") in UPLOAD_EXTENSIONS):
        return base
    suffix = f".{ext.lstrip('.')}" if ext else Path(download_name).suffix.lower()
    return f"{base}{suffix}"


def skip_reason(node: dict, settings, runtime: str) -> str:
    ext = node_extension(node)
    if f".{ext}" in settings.sync_skip_ext_list:
        return "扩展名在跳过列表，不处理"
    if online_type(node) or not ext:
        return ""
    if runtime == "library":
        allowed = LIBRARY_EXTENSIONS
        hint = "文档库"
    elif runtime == "rag_pipeline":
        allowed = UPLOAD_EXTENSIONS
        hint = "流水线文件上传"
    else:
        allowed = supported_dify_extensions(settings.dify_etl_type)
        hint = f"ETL={settings.dify_etl_type}"
    if ext not in allowed:
        return f"目标知识库不支持 .{ext} 格式（{hint}）"
    return ""


def fetch_source_file(dt, node: dict, output_dir: Path) -> Path:
    """目录与指定文档同步共用：类型来自钉钉节点，普通文件原样下载。"""
    from .export_service import export_online_doc
    from kb_common.config import get_settings
    output_dir.mkdir(parents=True, exist_ok=True)
    name = node.get("name") or node["nodeId"]
    ext = node_extension(node)
    online = online_type(node)
    if online:
        export_name = name if name.lower().endswith(f".{online}") else f"{name}.{online}"
        return export_online_doc(node["nodeId"], export_name, output_dir,
                                 get_settings().sync_export_timeout_seconds)
    content, download_name = dt.download_document(node["nodeId"])
    expected_size = node.get("size")
    if expected_size and int(expected_size) != len(content):
        raise ValueError(f"原文件大小与钉钉节点不一致（预期 {expected_size} 字节，实际 {len(content)} 字节），请刷新后重试")
    path = output_dir / source_file_name(name, ext, download_name)
    path.write_bytes(content)
    return path


def download_single_source(node_id: str) -> tuple[str, bytes, str]:
    import tempfile
    from .sync_database import SyncSessionLocal
    from .sync_settings import make_dingtalk_client
    with SyncSessionLocal() as db:
        dt = make_dingtalk_client(db)
    try:
        response = dt.get_node(node_id)
        node = response.get("node") or response
        if not node.get("name"):
            raise ValueError("钉钉未返回源文件元数据，无法确定原文件类型")
        node = {**node, "nodeId": node_id}
        with tempfile.TemporaryDirectory(prefix="dingtalk-source-") as tmp:
            path = fetch_source_file(dt, node, Path(tmp))
            return path.name, path.read_bytes(), "export" if online_type(node) else "file"
    finally:
        dt.close()
