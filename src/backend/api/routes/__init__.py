"""API routes.

v1 routes are registered via the edition registry in ``api.routes.v1`` (seam C1);
this package only keeps the re-export of non-v1 file download routes.
"""

from .files import router as files_router
from .sites_serve import router as sites_serve_router

__all__ = ["files_router", "sites_serve_router"]
