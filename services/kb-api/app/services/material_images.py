"""Image contracts and deterministic preprocessing. No model is loaded here."""
import asyncio
import base64
import io
import math
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import Literal
from uuid import UUID

MAX_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 20_000_000
PIPELINE_VERSION = "image-v1"
DASHSCOPE_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"


class ComponentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["embedding", "remover"]
    provider: Literal["dashscope", "http"]
    endpoint: str = Field(max_length=1000)
    model: str = Field(default="", max_length=200)
    dimensions: int = Field(default=1024, ge=64, le=4096)
    api_key: str = Field(default="", max_length=4000)

    @field_validator("name", "endpoint", "model", "api_key")
    @classmethod
    def strip(cls, v):
        return v.strip()

    @model_validator(mode="after")
    def validate_component(self):
        url = urlsplit(self.endpoint)
        if not self.name or url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.fragment or url.query:
            raise ValueError("填写有效的 HTTP 接口地址，不要在地址中携带密钥")
        if self.kind == "embedding" and not self.model:
            raise ValueError("请填写图像向量模型名称")
        if self.provider == "dashscope":
            if self.kind != "embedding" or not self.api_key:
                raise ValueError("百炼图像向量组件需要 API Key")
            if url.scheme != "https" or url.hostname not in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com"}:
                raise ValueError("百炼组件需使用官方 HTTPS 接口地址")
            dims = {
                "multimodal-embedding-v1": {1024},
                "qwen3-vl-embedding": {2560, 2048, 1536, 1024, 768, 512, 256},
                "qwen2.5-vl-embedding": {2048, 1024, 768, 512},
                "tongyi-embedding-vision-plus": {1152},
                "tongyi-embedding-vision-flash": {768},
                "tongyi-embedding-vision-plus-2026-03-06": {1152, 1024, 512, 256, 128, 64},
                "tongyi-embedding-vision-flash-2026-03-06": {768, 512, 256, 128, 64},
            }
            if self.model not in dims or self.dimensions not in dims[self.model]:
                raise ValueError("百炼模型名称或向量维度不受支持，请核对模型说明")
        return self


class LibraryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    embedding_id: UUID | None = None
    remover_id: UUID | None = None
    preprocess: bool = True


class LibraryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    enabled: bool = True
    config: LibraryConfig = Field(default_factory=LibraryConfig)

    @field_validator("name")
    @classmethod
    def name_required(cls, v):
        if not v.strip():
            raise ValueError("名称不能为空")
        return v.strip()


class MaterialIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=200)
    specification: str = Field(default="", max_length=500)
    category: str = Field(default="", max_length=120)
    tags: list[str] = Field(default_factory=list, max_length=20)
    description: str = Field(default="", max_length=10000)
    enabled: bool = True

    @field_validator("code", "name")
    @classmethod
    def required(cls, v):
        if not v.strip():
            raise ValueError("料号和名称不能为空")
        return v.strip()

    @field_validator("tags")
    @classmethod
    def valid_tags(cls, tags):
        if any(len(t) > 64 for t in tags):
            raise ValueError("单个标签不超过 64 字")
        return list(dict.fromkeys(t.strip() for t in tags if t.strip()))


class ImageEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    material_id: UUID | None = None


class RetryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    use_library_config: bool = False
    preprocess: bool | None = None
    crop: list[int] | None = None


def decode_image(raw: bytes) -> Image.Image:
    if len(raw) > MAX_BYTES:
        raise ValueError("图片不能超过 20MB")
    try:
        img = Image.open(io.BytesIO(raw))
        if img.format not in {"JPEG", "PNG", "WEBP"} or getattr(img, "n_frames", 1) != 1:
            raise ValueError("仅支持静态 JPG、PNG、WebP 图片")
        if img.width * img.height > MAX_PIXELS or min(img.size) < 16:
            raise ValueError("图片至少 16×16 像素，且不超过 2000 万像素")
        img.load()
        return ImageOps.exif_transpose(img).convert("RGBA")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("图片损坏或格式不受支持，请重新上传") from exc


def crop_image(img: Image.Image, crop: list[int] | None) -> Image.Image:
    if crop is None:
        return img
    if len(crop) != 4 or any(type(x) is not int for x in crop):
        raise ValueError("主体框需为 [左, 上, 右, 下] 像素坐标")
    left, top, right, bottom = crop
    if not (0 <= left < right <= img.width and 0 <= top < bottom <= img.height and right-left >= 16 and bottom-top >= 16):
        raise ValueError("主体框超出图片范围或小于 16×16 像素")
    return img.crop(tuple(crop))


def png(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def standardize(img: Image.Image, *, removed: bool) -> bytes:
    alpha = img.getchannel("A")
    mask = alpha.point(lambda p: 255 if p > 32 else 0)
    bbox = mask.getbbox()
    if not bbox:
        raise ValueError("抠图结果为空，请重新框选主体或显式关闭图片预加工")
    if removed:
        hist = mask.histogram()
        fraction = hist[255] / (img.width * img.height)
        if fraction < 0.001 or alpha.getextrema()[0] == 255:
            raise ValueError("抠图结果异常：主体过小或未移除背景，请检查组件或重新框选")
        img = img.crop(bbox)
    # Preserve aspect ratio and all interior holes; never flood-fill or inpaint.
    scale = min(920 / img.width, 920 / img.height)
    img = img.resize((max(1, round(img.width*scale)), max(1, round(img.height*scale))), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (1024, 1024), (245, 245, 245))
    canvas.paste(img, ((1024-img.width)//2, (1024-img.height)//2), img)
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=92)
    return out.getvalue()


def data_uri(data: bytes, mime="image/jpeg") -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


async def request_component(component, **kwargs) -> httpx.Response:
    headers = {"Authorization": f"Bearer {component.api_key}"} if component.api_key else {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=10), follow_redirects=False) as client:
            async with client.stream("POST", component.endpoint, headers=headers, **kwargs) as response:
                if response.status_code >= 300:
                    if response.status_code in {401, 403}:
                        raise ValueError("模型服务鉴权失败，请在图片组件配置中更新密钥与权限")
                    if response.status_code == 429:
                        raise ValueError("模型服务额度不足或请求过于频繁，请检查配额后重试")
                    raise ValueError(f"模型服务返回 HTTP {response.status_code}，请检查接口协议及模型名称")
                chunks, size = [], 0
                async for part in response.aiter_bytes():
                    size += len(part)
                    if size > MAX_BYTES:
                        raise ValueError("模型服务返回内容超过 20MB")
                    chunks.append(part)
                # aiter_bytes already decompresses the upstream body. Do not
                # retain encoding/length headers and decode it a second time.
                decoded_headers = {k: v for k, v in response.headers.items()
                                   if k.lower() not in {"content-encoding", "content-length", "transfer-encoding"}}
                return httpx.Response(response.status_code, headers=decoded_headers, content=b"".join(chunks))
    except httpx.TimeoutException as exc:
        raise ValueError("模型服务调用超时，请稍后重试") from exc
    except httpx.HTTPError as exc:
        raise ValueError("模型服务无法连接，请检查组件地址") from exc


async def remove_background(img: Image.Image, component) -> Image.Image:
    response = await request_component(component,
        files={"file": ("image.png", await asyncio.to_thread(png, img), "image/png")},
        data={"model": component.model})
    try:
        if "application/json" in response.headers.get("content-type", ""):
            raw = base64.b64decode(response.json()["image_base64"].split(",")[-1], validate=True)
        else:
            raw = response.content
        result = await asyncio.to_thread(decode_image, raw)
        if result.size != img.size:
            raise ValueError("抠图组件必须返回与输入同尺寸的透明 PNG")
        return result
    except (KeyError, TypeError) as exc:
        raise ValueError("抠图组件应返回透明 PNG 或 image_base64 字段") from exc


async def prepare(raw: bytes, remover=None, crop=None) -> tuple[bytes, bytes | None]:
    img = await asyncio.to_thread(decode_image, raw)
    img = crop_image(img, crop)
    if remover is not None:
        img = await remove_background(img, remover)
    standard = await asyncio.to_thread(standardize, img.copy(), removed=remover is not None)
    foreground = await asyncio.to_thread(png, img) if remover is not None else None
    return standard, foreground


def validate_vector(vector, dimensions: int) -> list[float]:
    if not isinstance(vector, list) or len(vector) != dimensions:
        raise ValueError("图像向量维度与组件配置不一致")
    if any(type(v) not in {int, float} or not math.isfinite(v) for v in vector):
        raise ValueError("图像向量包含无效数值")
    norm = math.sqrt(sum(v*v for v in vector))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("图像向量为空或无效")
    return [v / norm for v in vector]


async def embed(standard: bytes, component) -> list[float]:
    image = data_uri(standard)
    if component.provider == "dashscope":
        payload = {"model": component.model, "input": {"contents": [{"image": image}]}}
        if component.model not in {"multimodal-embedding-v1", "tongyi-embedding-vision-plus", "tongyi-embedding-vision-flash"}:
            payload["parameters"] = {"dimension": component.dimensions}
    else:
        payload = {"model": component.model, "input": [{"image": image}], "dimensions": component.dimensions}
    response = await request_component(component, json=payload)
    try:
        body = response.json()
        rows = body["output"]["embeddings"] if component.provider == "dashscope" else body["data"]
        if len(rows) != 1:
            raise ValueError("图像向量服务返回数量不正确")
        return validate_vector(rows[0]["embedding"], component.dimensions)
    except (KeyError, TypeError, IndexError) as exc:
        raise ValueError("图像向量响应格式不正确，请检查组件协议") from exc


def index_name(component_id) -> str:
    return "material_image_v1_" + str(component_id).replace("-", "")


def entry_id(image) -> str:
    return f"{image.id}:{image.run_id}"


def group_key(image) -> tuple:
    return (image.embedding_id, image.remover_id if image.preprocess else None, image.pipeline_version)


def aggregate(ranked_groups: list[list[dict]], top_k: int) -> list[dict]:
    """Deduplicate views BEFORE RRF: a material with more photos gets no vote bonus."""
    merged = {}
    for hits in ranked_groups:
        seen = set()
        rank = 0
        for hit in hits:
            key = hit.get("material_id") or hit["image_id"]
            if key in seen:
                continue
            seen.add(key)
            rank += 1
            if key not in merged:
                merged[key] = {**hit, "rank_score": 0.0}
            merged[key]["rank_score"] += 1 / (60 + rank)
    return sorted(merged.values(), key=lambda h: (-h["rank_score"], h["image_id"]))[:top_k]
