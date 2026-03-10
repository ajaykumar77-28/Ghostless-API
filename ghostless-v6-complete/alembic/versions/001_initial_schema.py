"""Initial schema

Revision ID: 001
Create Date: 2025-01-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Run the app's metadata create_all via SQLAlchemy
    # (In practice, autogenerate from models)
    pass


def downgrade() -> None:
    pass
