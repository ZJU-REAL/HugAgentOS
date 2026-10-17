"""Allow audio model providers without changing existing model assignments."""
from alembic import op
import sqlalchemy as sa

revision = "ce_0027"
down_revision = "ce_0026"
branch_labels = None
depends_on = None


def _constraint(types):
    with op.batch_alter_table("model_providers") as batch:
        batch.drop_constraint("model_providers_type_check", type_="check")
        batch.create_check_constraint("model_providers_type_check", f"provider_type IN ({types})")


def upgrade():
    from core.db.audio_model_upgrade import upgrade_audio_model_type
    upgrade_audio_model_type(op.get_bind())


def downgrade():
    if op.get_bind().execute(sa.text("SELECT count(*) FROM model_providers WHERE provider_type = 'audio'")).scalar():
        raise RuntimeError("Remove audio providers through model management before downgrade; no configuration is deleted automatically")
    _constraint("'chat', 'embedding', 'reranker'")
