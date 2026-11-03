"""Row level security on every table when running on PostgreSQL (Supabase)

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-03 14:00:00

Supabase exposes tables in the public schema through its automatic REST API. With RLS on and no policies, that API
returns nothing, while Lead Desk connects as the table owner and is unaffected. New tables need the same line.
"""
from alembic import op
import sqlalchemy as sa

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def _tables():
    return [t for t in sa.inspect(op.get_bind()).get_table_names() if t != 'alembic_version'] + ['alembic_version']


def upgrade() -> None:
    if op.get_bind().dialect.name != 'postgresql':
        return
    for table in _tables():
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    if op.get_bind().dialect.name != 'postgresql':
        return
    for table in _tables():
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
