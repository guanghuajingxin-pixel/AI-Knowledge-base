"""Bind unified identities without changing business user ids.

Revision ID: 0052_user_oidc_identities
Revises: 0051_lib_doc_knowledge_center
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0052_user_oidc_identities'
down_revision = '0051_lib_doc_knowledge_center'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('user_identities',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('issuer', sa.String(500), nullable=False),
        sa.Column('subject', sa.String(255), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('issuer', 'subject', name='uq_user_identity_subject'),
        sa.UniqueConstraint('issuer', 'user_id', name='uq_user_identity_user'))
    op.create_index('ix_user_identities_user_id', 'user_identities', ['user_id'])


def downgrade():
    op.drop_index('ix_user_identities_user_id', table_name='user_identities')
    op.drop_table('user_identities')
