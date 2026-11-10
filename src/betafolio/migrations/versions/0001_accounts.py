"""Initial production schema; no single-owner data is imported."""
from alembic import op
import sqlalchemy as sa

revision = '0001_accounts'
down_revision = None
branch_labels = depends_on = None


def upgrade():
    op.create_table('users',
        sa.Column('id',sa.String(36),primary_key=True),sa.Column('email',sa.String(254),nullable=False,unique=True),
        sa.Column('password_hash',sa.Text(),nullable=False),sa.Column('role',sa.String(12),nullable=False),
        sa.Column('verified',sa.Boolean(),nullable=False),sa.Column('suspended',sa.Boolean(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),sa.Column('last_active',sa.DateTime(timezone=True)),
        sa.Column('timezone',sa.String(80),nullable=False),sa.Column('schedule_opt_in',sa.Boolean(),nullable=False),
        sa.Column('email_opt_in',sa.Boolean(),nullable=False),sa.Column('allowance',sa.Integer()),
        sa.Column('portfolio',sa.JSON()),sa.Column('profile',sa.JSON()),sa.Column('report_length',sa.String(12),nullable=False),
        sa.Column('mfa_secret',sa.Text()),sa.Column('pending_mfa',sa.Text()),sa.Column('recovery_hashes',sa.JSON(),nullable=False),
        sa.Column('mfa_last_counter',sa.Integer(),nullable=False))
    for table in ('sessions','auth_tokens'):
        columns = [sa.Column('token_hash',sa.String(64),primary_key=True),
            sa.Column('user_id',sa.String(36),sa.ForeignKey('users.id',ondelete='CASCADE'),nullable=False),
            sa.Column('expires',sa.DateTime(timezone=True),nullable=False)]
        columns.append(sa.Column('mfa',sa.Boolean(),nullable=False) if table=='sessions' else sa.Column('kind',sa.String(20),nullable=False))
        op.create_table(table,*columns)
        op.create_index('ix_'+table+'_user_id',table,['user_id'])
    op.create_table('app_state',sa.Column('key',sa.String(80),primary_key=True),sa.Column('value',sa.JSON(),nullable=False))
    op.create_table('credentials',sa.Column('name',sa.String(80),primary_key=True),
        sa.Column('ciphertext',sa.Text(),nullable=False),sa.Column('saved_at',sa.DateTime(timezone=True),nullable=False))
    op.create_table('jobs',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('user_id',sa.String(36),sa.ForeignKey('users.id',ondelete='CASCADE'),nullable=False),
        sa.Column('kind',sa.String(20),nullable=False),sa.Column('status',sa.String(20),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),sa.Column('started_at',sa.DateTime(timezone=True)),
        sa.Column('finished_at',sa.DateTime(timezone=True)),sa.Column('heartbeat',sa.DateTime(timezone=True)),
        sa.Column('worker_id',sa.String(36)),sa.Column('schedule_key',sa.String(80)),sa.Column('allowance_day',sa.String(10)),
        sa.Column('consumed',sa.Boolean(),nullable=False),sa.Column('cancel_requested',sa.Boolean(),nullable=False),
        sa.Column('payload',sa.JSON(),nullable=False),sa.Column('result',sa.JSON()),sa.Column('activity',sa.JSON(),nullable=False),
        sa.Column('budget',sa.JSON(),nullable=False),sa.Column('error_code',sa.String(50)),sa.Column('delivery',sa.String(20)),
        sa.Column('model_calls',sa.Integer(),nullable=False),sa.Column('searches',sa.Integer(),nullable=False),
        sa.Column('input_tokens',sa.Integer()),sa.Column('output_tokens',sa.Integer()),sa.Column('estimated_cost',sa.Float()),
        sa.UniqueConstraint('user_id','schedule_key',name='uq_user_schedule'))
    op.create_index('ix_jobs_user_id','jobs',['user_id'])
    op.create_index('ix_jobs_status','jobs',['status'])
    op.create_table('audit',sa.Column('id',sa.String(36),primary_key=True),sa.Column('actor_id',sa.String(36)),
        sa.Column('action',sa.String(80),nullable=False),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    op.create_table('rate_buckets',sa.Column('key',sa.String(64),primary_key=True),
        sa.Column('count',sa.Integer(),nullable=False),sa.Column('expires',sa.DateTime(timezone=True),nullable=False))
    state = sa.table('app_state',sa.column('key',sa.String()),sa.column('value',sa.JSON()))
    op.bulk_insert(state,[{'key':'queue_lock','value':{}}])


def downgrade():
    for table in ('rate_buckets','audit','jobs','credentials','app_state','auth_tokens','sessions','users'):
        op.drop_table(table)
