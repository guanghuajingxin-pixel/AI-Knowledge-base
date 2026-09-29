"""Lazy object/vector clients keep API import independent of infrastructure."""
import asyncio
from elasticsearch import BadRequestError, NotFoundError
from .material_images import index_name, entry_id


async def put(key: str, raw: bytes, mime: str):
    from kb_common.clients import minio_client as m
    await asyncio.to_thread(m.upload_bytes, m.RAW, key, raw, mime)


async def get(key: str) -> bytes:
    from kb_common.clients import minio_client as m
    def read():
        response = m.minio.get_object(m.RAW, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()
    return await asyncio.to_thread(read)


async def remove_prefix(prefix: str):
    from kb_common.clients import minio_client as m
    # Surface object deletion errors; original remains recoverable on failure.
    def remove():
        objects = m.minio.list_objects(m.RAW, prefix=prefix, recursive=True)
        for obj in objects:
            m.minio.remove_object(m.RAW, obj.object_name)
    await asyncio.to_thread(remove)


async def index_image(image, vector):
    from kb_common.clients.es_client import es
    name = index_name(image.embedding_id)
    if not await es.indices.exists(index=name):
        try:
            await es.indices.create(index=name, mappings={"properties": {
                "vector": {"type": "dense_vector", "dims": len(vector), "index": True, "similarity": "cosine"},
                "entry_id": {"type": "keyword"}, "image_id": {"type": "keyword"},
                "library_id": {"type": "integer"}}})
        except BadRequestError as exc:
            if exc.error != "resource_already_exists_exception":
                raise
    await es.index(index=name, id=entry_id(image), document={
        "vector": vector, "entry_id": entry_id(image), "image_id": str(image.id),
        "library_id": image.library_id}, refresh="wait_for")


async def delete_vector(image):
    from kb_common.clients.es_client import es
    try:
        await es.delete_by_query(index=index_name(image.embedding_id),
            query={"term": {"image_id": str(image.id)}}, refresh=True, conflicts="proceed")
    except NotFoundError:
        pass


async def nearest(component_id, library_id, vector, images, top_k):
    from kb_common.clients.es_client import es
    # At most 20 views/material: fetch enough neighbors to avoid one SKU dominating.
    k = min(len(images), top_k * 20)
    if not k:
        return []
    response = await es.search(index=index_name(component_id), size=k, source=["entry_id"],
        knn={"field": "vector", "query_vector": vector, "k": k,
             "num_candidates": min(max(100, k*4), 10000),
             "filter": {"bool": {"filter": [
                 {"term": {"library_id": library_id}},
                 {"terms": {"entry_id": [entry_id(i) for i in images]}}]}}})
    return [(h["_source"]["entry_id"], 2*h["_score"]-1) for h in response["hits"]["hits"]]
