"""add keyword type and search volume

Revision ID: 0002_keyword_metadata
Revises: 0001_initial
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_keyword_metadata"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("keywords", sa.Column("keyword_type", sa.String(length=30), nullable=True))
    op.add_column("keywords", sa.Column("search_volume", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("keywords", "search_volume")
    op.drop_column("keywords", "keyword_type")
