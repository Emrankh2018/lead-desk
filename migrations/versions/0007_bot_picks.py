"""Telegram bot: leads offered in the daily list and the owner's choice

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-03 10:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('bot_picks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('business_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('offered_on', sa.Date(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('business_id')
    )
    op.create_index('ix_bot_picks_offered_on', 'bot_picks', ['offered_on'])


def downgrade() -> None:
    op.drop_index('ix_bot_picks_offered_on', table_name='bot_picks')
    op.drop_table('bot_picks')
