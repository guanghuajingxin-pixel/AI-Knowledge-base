"""Material libraries: originals, immutable component revisions and image jobs."""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class ImageComponent(Base):
    __tablename__ = "image_components"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20))
    provider: Mapped[str] = mapped_column(String(32))
    endpoint: Mapped[str] = mapped_column(String(1000))
    model: Mapped[str] = mapped_column(String(200), default="")
    dimensions: Mapped[int] = mapped_column(Integer, default=1024)
    api_key: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Material(Base):
    __tablename__ = "materials"
    __table_args__ = (UniqueConstraint("library_id", "code", name="uq_material_library_code"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[int] = mapped_column(ForeignKey("knowledge_libraries.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(200))
    specification: Mapped[str] = mapped_column(String(500), default="")
    category: Mapped[str] = mapped_column(String(120), default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MaterialImage(Base):
    __tablename__ = "material_images"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[int] = mapped_column(ForeignKey("knowledge_libraries.id", ondelete="CASCADE"), index=True)
    material_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("materials.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(500))
    original_path: Mapped[str] = mapped_column(String(1000))
    foreground_path: Mapped[str | None] = mapped_column(String(1000))
    standard_path: Mapped[str | None] = mapped_column(String(1000))
    content_hash: Mapped[str] = mapped_column(String(64))
    embedding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("image_components.id"))
    remover_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("image_components.id"))
    preprocess: Mapped[bool] = mapped_column(Boolean, default=True)
    crop: Mapped[list | None] = mapped_column(JSON)
    pipeline_version: Mapped[str] = mapped_column(String(32), default="image-v1")
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(24), default="PENDING", index=True)
    message: Mapped[str] = mapped_column(String(500), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
