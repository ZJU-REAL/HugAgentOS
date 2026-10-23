"""API routes.

v1 routes are registered via the edition registry in ``api.routes.v1`` (seam C1);
this package only keeps the re-export of non-v1 file download routes, resolved
lazily so that importing a sibling module (``api.routes.file_location``) does not
load ``files`` and the feature routers it mounts.
"""

__all__ = ["files_router"]


def __getattr__(name):
    if name == "files_router":
        from api.routes.files import router

        return router
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
