"""Personal file mounts for the community distribution."""
workspace_mount_names = ("myspace", "skills")

def team_grants(db, actor):
    return ()

def session_entrypoint(entrypoint, user_id):
    return list(entrypoint)

def project_location(db, project):
    return None

def matches_scope(project, scope):
    return scope.kind == project.kind and scope.root_folder_id == project.linked_folder_id

def owner_space(column, owner):
    raise ValueError("Unsupported directory owner")
