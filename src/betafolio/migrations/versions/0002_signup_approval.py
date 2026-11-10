"""Separate access requests from accounts and queue approval emails durably."""
from alembic import op
import sqlalchemy as sa

revision = '0002_signup_approval'
down_revision = '0001_accounts'
branch_labels = depends_on = None


def upgrade():
    op.create_table('signup_requests',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('email', sa.String(254), nullable=False, unique=True),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('approved_at', sa.DateTime(timezone=True)),
        sa.Column('token_hash', sa.String(64), unique=True),
        sa.Column('expires', sa.DateTime(timezone=True)))
    op.alter_column('jobs', 'user_id', existing_type=sa.String(36), nullable=True)
    op.add_column('jobs', sa.Column('signup_request_id', sa.String(36),
                                  sa.ForeignKey('signup_requests.id', ondelete='CASCADE', name='fk_job_signup_request')))
    op.create_index('ix_jobs_signup_request_id', 'jobs', ['signup_request_id'])
    op.create_check_constraint('ck_job_owner', 'jobs',
        "(kind = 'signup_email' AND signup_request_id IS NOT NULL AND user_id IS NULL) OR "
        "(kind <> 'signup_email' AND user_id IS NOT NULL AND signup_request_id IS NULL)")


def downgrade():
    op.execute("DELETE FROM jobs WHERE kind = 'signup_email'")
    op.drop_constraint('ck_job_owner', 'jobs', type_='check')
    op.drop_index('ix_jobs_signup_request_id', table_name='jobs')
    op.drop_constraint('fk_job_signup_request', 'jobs', type_='foreignkey')
    op.drop_column('jobs', 'signup_request_id')
    op.alter_column('jobs', 'user_id', existing_type=sa.String(36), nullable=False)
    op.drop_table('signup_requests')
