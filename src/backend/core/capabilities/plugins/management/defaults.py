"""Add shared defaults without replaying an edition's earlier onboarding."""

from core.db.models import ContentBlock, InstalledPlugin
from core.capabilities.plugins.management.installation import install_plugin
from sqlalchemy.orm import Session

VISUALIZE_BOOTSTRAP_MARKER_ID = "visualize_plugin_bootstrap_v1"


def ensure_visualize_plugin_bootstrapped(db: Session) -> bool:
    """Install once in CE/EE, including databases with old onboarding markers.

    An independent marker preserves later removal. Existing installations are
    retained, including their component switches and administrator changes.
    """
    if db.get(ContentBlock, VISUALIZE_BOOTSTRAP_MARKER_ID) is not None:
        return False
    try:
        if db.get(InstalledPlugin, "visualize@global") is None:
            install_plugin(db, "visualize", owner_user_id=None, created_by="system_bootstrap")
        db.add(
            ContentBlock(
                id=VISUALIZE_BOOTSTRAP_MARKER_ID,
                payload={"version": 1, "plugins": ["visualize"]},
                updated_by="system_bootstrap",
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return True
