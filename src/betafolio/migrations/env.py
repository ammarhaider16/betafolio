"""Alembic runs on the transaction provided by the operator CLI."""
from alembic import context
from betafolio.database import Base

connection = context.config.attributes['connection']
context.configure(connection=connection,target_metadata=Base.metadata)
with context.begin_transaction():
    context.run_migrations()
