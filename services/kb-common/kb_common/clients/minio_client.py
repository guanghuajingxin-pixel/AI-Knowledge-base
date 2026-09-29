import io
from datetime import timedelta
from minio import Minio
from minio.commonconfig import CopySource
from minio.deleteobjects import DeleteObject
from kb_common.config import get_settings
_s = get_settings()
minio = Minio(_s.minio_endpoint, access_key=_s.minio_access_key,
              secret_key=_s.minio_secret_key, secure=False)
RAW = "raw-docs"; PARSED = "parsed-docs"
for b in (RAW, PARSED):
    if not minio.bucket_exists(b): minio.make_bucket(b)


def upload_bytes(bucket: str, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """上传字节到 Minio，返回对象 key。"""
    minio.put_object(bucket, key, io.BytesIO(data), len(data), content_type=content_type)
    return key


def copy_object(src_bucket: str, src_key: str, dst_bucket: str, dst_key: str) -> str:
    """服务端拷贝对象（同集群内迁移，如暂存区 → 正式路径），返回目标 key。"""
    minio.copy_object(dst_bucket, dst_key, CopySource(src_bucket, src_key))
    return dst_key


def delete_prefix(bucket: str, prefix: str) -> int:
    """删除前缀下全部对象，返回删除数量（前缀不存在时为 0，幂等）。"""
    names = [o.object_name for o in minio.list_objects(bucket, prefix=prefix, recursive=True)]
    if names:
        for _err in minio.remove_objects(bucket, [DeleteObject(n) for n in names]):
            pass  # remove_objects 惰性返回错误，消费以触发删除
    return len(names)


def presigned_url(bucket: str, key: str, expires: timedelta = timedelta(hours=24)) -> str:
    """生成预签名下载 URL。"""
    return minio.presigned_get_object(bucket, key, expires=expires)
