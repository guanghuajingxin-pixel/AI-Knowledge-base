"""Material libraries, image components, and durable image processing records."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0049_material_libraries"
down_revision = "0048_chunk_child_index"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("image_components",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("endpoint", sa.String(1000), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("api_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False))
    op.create_table("materials",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("library_id", sa.Integer(), sa.ForeignKey("knowledge_libraries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code", sa.String(120), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("specification", sa.String(500), nullable=False),
        sa.Column("category", sa.String(120), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("library_id", "code", name="uq_material_library_code"))
    op.create_index("ix_materials_library_id", "materials", ["library_id"])
    op.create_table("material_images",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("library_id", sa.Integer(), sa.ForeignKey("knowledge_libraries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("material_id", UUID(as_uuid=True), sa.ForeignKey("materials.id", ondelete="CASCADE")),
        sa.Column("filename", sa.String(500), nullable=False),
        sa.Column("original_path", sa.String(1000), nullable=False),
        sa.Column("foreground_path", sa.String(1000)),
        sa.Column("standard_path", sa.String(1000)),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("embedding_id", UUID(as_uuid=True), sa.ForeignKey("image_components.id"), nullable=False),
        sa.Column("remover_id", UUID(as_uuid=True), sa.ForeignKey("image_components.id")),
        sa.Column("preprocess", sa.Boolean(), nullable=False),
        sa.Column("crop", sa.JSON()),
        sa.Column("pipeline_version", sa.String(32), nullable=False),
        sa.Column("run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("message", sa.String(500), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False))
    for column in ("library_id", "material_id", "status"):
        op.create_index("ix_material_images_" + column, "material_images", [column])


def downgrade():
    op.drop_table("material_images")
    op.drop_table("materials")
    op.drop_table("image_components")
