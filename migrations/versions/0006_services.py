"""services on profiles, offers and drafts

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-02 10:00:00

Existing profiles and offers were all built for AI product ads, so they are backfilled with that service. The new
default profile and offers for websites and AI assistants are added by leadgen.services.seed, not here.
"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('icp_profiles', sa.Column('services', sa.JSON(), nullable=False, server_default='[]'))
    op.add_column('offers', sa.Column('service', sa.String(length=30), nullable=True))
    op.add_column('outreach_drafts', sa.Column('service', sa.String(length=30), nullable=True))
    op.execute("UPDATE icp_profiles SET services = '[\"product_ads\"]'")
    op.execute("UPDATE offers SET service = 'product_ads'")
    op.execute("UPDATE outreach_drafts SET service = 'product_ads'")


def downgrade() -> None:
    op.drop_column('outreach_drafts', 'service')
    op.drop_column('offers', 'service')
    op.drop_column('icp_profiles', 'services')
