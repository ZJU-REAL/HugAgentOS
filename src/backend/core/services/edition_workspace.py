"""Community Edition has no additional workspace volumes."""

from fastapi import HTTPException


def resolve_workspaces(actor):
    return []


def persist_user(actor):
    return 0


def project_directory(scope):
    return ""


def project_directory_by_id(project_id):
    return ""


def workspace_instructions():
    return ""


def is_organization_path(scope, actor, path):
    return False


def write_organization_text(*args, **kwargs):
    raise HTTPException(400, "Unsupported workspace kind")


def resolve_alias(path):
    return None


def resolve_project_path(path, user_id, root, scope):
    return None


def is_shared_scope(scope):
    return False


def context_directory(context):
    return ""


def source_directory(project):
    return ""


def folder_scope_label(kind):
    return "我的空间"


def command_instructions(scope):
    return ""


def refresh_artifact(item, file_id, db, actor=None):
    return None


def delete_organization_path(*args, **kwargs):
    raise HTTPException(400, "Unsupported workspace kind")
