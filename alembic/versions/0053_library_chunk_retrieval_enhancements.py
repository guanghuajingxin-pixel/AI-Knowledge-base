"""Persist generated retrieval enhancements per library chunk."""
from alembic import op
import sqlalchemy as sa


revision = "0053_chunk_enhance"
down_revision = "0052_user_oidc_identities"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "library_chunks",
        sa.Column("retrieval_enhancements", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
    )
    op.alter_column("library_chunks", "retrieval_enhancements", server_default=None)


def downgrade():
    op.drop_column("library_chunks", "retrieval_enhancements")
