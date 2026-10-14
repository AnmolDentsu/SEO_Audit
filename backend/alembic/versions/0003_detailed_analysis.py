"""detailed content structure and analysis cache

Revision ID: 0003_detailed_analysis
Revises: 0002_keyword_metadata
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_detailed_analysis"
down_revision = "0002_keyword_metadata"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("analysis_json", sa.JSON(), nullable=True))
    with op.batch_alter_table("page_results") as batch:
        batch.add_column(sa.Column("canonical_url", sa.Text(), nullable=True))
        batch.add_column(sa.Column("extraction_root", sa.String(length=50), nullable=True))
        batch.add_column(sa.Column("paragraph_count", sa.Integer(), nullable=True, server_default="0"))
        batch.add_column(sa.Column("structure_json", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("page_results") as batch:
        batch.drop_column("structure_json")
        batch.drop_column("paragraph_count")
        batch.drop_column("extraction_root")
        batch.drop_column("canonical_url")
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("analysis_json")
