"""Personal Agent workspace routes."""

from fastapi import APIRouter, Depends, HTTPException, Query

from core.infra.responses import success_response

from personal_agent.api.schemas import WorkspaceFile
from personal_agent.api.dependencies import service
from personal_agent.api.dependencies import perform

router = APIRouter()


def workspace_call(fn):
    """Workspace views for My Space; a backend without the volume reports it as unavailable."""
    from personal_agent.workspace.files import WorkspaceUnavailable
    from personal_agent.workspace.persona import ensure_seeded

    try:
        return fn(ensure_seeded)
    except WorkspaceUnavailable:
        return success_response(data={"available": False})
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None
    except (IsADirectoryError, NotADirectoryError):
        raise HTTPException(400, "Path is not a file") from None


@router.get("/workspace/tree")
def workspace_tree(svc=Depends(service)):
    from personal_agent.workspace.files import tree

    agent = svc.identity.ensure()

    def run(seed):
        seed(agent["id"])
        return success_response(data={"available": True, **tree(agent["id"])})

    return workspace_call(run)


@router.get("/workspace/file")
def workspace_read(path: str = Query(min_length=1, max_length=512), svc=Depends(service)):
    from personal_agent.workspace.files import read_file

    agent = svc.identity.ensure()
    return workspace_call(
        lambda seed: perform(lambda: {"available": True, **read_file(agent["id"], path)})
    )


@router.put("/workspace/file")
def workspace_write(body: WorkspaceFile, svc=Depends(service)):
    from personal_agent.workspace.constants import WORKSPACE
    from personal_agent.workspace.files import write_file

    agent = svc.identity.ensure()

    def run():
        saved = write_file(agent["id"], body.path, body.content, body.etag)
        # A hand-edited name line in IDENTITY.md renames the agent everywhere.
        if saved["path"] == WORKSPACE + "/IDENTITY.md":
            svc.identity.adopt_identity_name(body.content)
        return {"available": True, **saved}

    return workspace_call(lambda seed: perform(run))
