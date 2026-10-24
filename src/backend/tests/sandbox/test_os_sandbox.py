"""Policy model and per-platform launch construction.

These assertions are about the properties that make the sandbox worth having:
a narrower rule beats a broader one, a writable root does not carry its own
metadata with it, the network is a dimension rather than an afterthought, and no
code path ever answers "could not confine" with an unconfined command.
"""

from __future__ import annotations

import os
import sys
import types
from collections import Counter
from unittest.mock import patch

import pytest
from core.sandbox.oslayer import (
    AccessMode,
    FileSystemEntry,
    FileSystemKind,
    FileSystemPolicy,
    NetworkPolicy,
    PolicyContext,
    SandboxLaunch,
    SandboxPolicy,
    SandboxUnavailableError,
    SandboxUnenforceableError,
    SpecialPath,
    build_filesystem_policy,
    build_launch,
    get_platform_backend,
    windows_runtime,
)
from core.sandbox.oslayer.bwrap import BubblewrapBackend, resolve_bwrap
from core.sandbox.oslayer.seatbelt import SeatbeltBackend
from core.sandbox.oslayer.windows_runtime import (
    TEMP_ENV_KEYS,
    LabeledRoots,
    child_environment,
    root_key,
)
from core.sandbox.oslayer.windows_token import (
    WindowsLowIntegrityBackend,
    build_plan,
    scratch_directory,
)


def _context(tmp_path, **overrides) -> PolicyContext:
    defaults = dict(
        cwd=str(tmp_path),
        workspace_roots=(str(tmp_path),),
        home_dir=str(tmp_path),
        temp_dirs=(str(tmp_path / "tmp"),),
        protected_metadata_names=(".git",),
        state_dir=str(tmp_path / "state"),
    )
    defaults.update(overrides)
    return PolicyContext(**defaults)


# ── Policy model ────────────────────────────────────────────────────────────


def test_narrower_entry_overrides_a_broader_one(tmp_path):
    """`/repo = write`, `/repo/a = deny`, `/repo/a/b = write` means what it reads like."""
    repo = tmp_path / "repo"
    (repo / "a" / "b").mkdir(parents=True)
    policy = FileSystemPolicy(
        kind=FileSystemKind.RESTRICTED,
        entries=(
            FileSystemEntry(access=AccessMode.WRITE, path=str(repo)),
            FileSystemEntry(access=AccessMode.DENY, path=str(repo / "a")),
            FileSystemEntry(access=AccessMode.WRITE, path=str(repo / "a" / "b")),
        ),
    )
    resolved = policy.resolve(_context(tmp_path))

    assert resolved.effective_access(str(repo)) is AccessMode.WRITE
    assert resolved.effective_access(str(repo / "a")) is AccessMode.DENY
    assert resolved.effective_access(str(repo / "a" / "c")) is AccessMode.DENY
    assert resolved.effective_access(str(repo / "a" / "b")) is AccessMode.WRITE


def test_the_more_restrictive_mode_wins_at_equal_specificity(tmp_path):
    target = tmp_path / "x"
    policy = FileSystemPolicy(
        entries=(
            FileSystemEntry(access=AccessMode.WRITE, path=str(target)),
            FileSystemEntry(access=AccessMode.DENY, path=str(target)),
        )
    )
    resolved = policy.resolve(_context(tmp_path))
    assert resolved.effective_access(str(target)) is AccessMode.DENY


def test_protected_metadata_is_carved_out_of_every_writable_root(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    resolved = build_filesystem_policy(writable_roots=[str(repo)]).resolve(_context(tmp_path))
    [root] = [item for item in resolved.roots_for(AccessMode.WRITE) if item.root == str(repo)]
    assert root.protected_metadata_names == (".git",)


def test_an_explicit_grant_on_protected_metadata_is_honoured(tmp_path):
    """Protection covers the names nobody asked to open, not a deliberate choice."""
    repo = tmp_path / "repo"
    repo.mkdir()
    policy = FileSystemPolicy(
        entries=(
            FileSystemEntry(access=AccessMode.WRITE, path=str(repo)),
            FileSystemEntry(access=AccessMode.WRITE, path=str(repo / ".git")),
        )
    )
    resolved = policy.resolve(_context(tmp_path))
    [root] = [item for item in resolved.roots_for(AccessMode.WRITE) if item.root == str(repo)]
    assert root.protected_metadata_names == ()


def test_scratch_space_is_marked_ephemeral(tmp_path):
    resolved = build_filesystem_policy(writable_roots=[str(tmp_path)]).resolve(_context(tmp_path))
    ephemeral = [item.root for item in resolved.roots_for(AccessMode.WRITE) if item.ephemeral]
    assert str(tmp_path / "tmp") in ephemeral


def test_narrowed_reads_request_the_platform_baseline(tmp_path):
    resolved = build_filesystem_policy(
        writable_roots=[str(tmp_path)], full_disk_read=False
    ).resolve(_context(tmp_path))
    assert resolved.include_platform_defaults is True
    assert resolved.has_full_disk_read_access is False


def test_a_policy_round_trips_through_json(tmp_path):
    policy = SandboxPolicy(
        filesystem=build_filesystem_policy(
            writable_roots=[str(tmp_path)], denied_paths=[str(tmp_path / "secret")]
        ),
        network=NetworkPolicy.RESTRICTED,
    )
    context = _context(tmp_path)
    assert SandboxPolicy.from_json(policy.to_json()) == policy
    assert PolicyContext.from_json(context.to_json()) == context


# ── Linux ───────────────────────────────────────────────────────────────────


def test_bwrap_isolates_namespaces_and_the_network(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    launch = BubblewrapBackend().build(
        SandboxPolicy(
            filesystem=build_filesystem_policy(writable_roots=[str(workspace)]),
            network=NetworkPolicy.RESTRICTED,
        ),
        _context(tmp_path),
    )
    argv = list(launch.argv_prefix)

    assert os.path.basename(argv[0]) == "bwrap"
    assert argv[-1] == "--"
    for flag in ("--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-net"):
        assert flag in argv
    assert argv[argv.index("--ro-bind") : argv.index("--ro-bind") + 3] == ["--ro-bind", "/", "/"]
    assert ["--bind", str(workspace), str(workspace)] == argv[
        argv.index("--bind") : argv.index("--bind") + 3
    ]


def test_bwrap_leaves_the_network_alone_when_the_policy_allows_it(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    launch = BubblewrapBackend().build(
        SandboxPolicy(
            filesystem=build_filesystem_policy(writable_roots=[str(workspace)]),
            network=NetworkPolicy.ENABLED,
        ),
        _context(tmp_path),
    )
    assert "--unshare-net" not in launch.argv_prefix


def test_bwrap_gives_scratch_space_a_private_tmpfs(tmp_path):
    """The host's shared temp directory is not a write channel the policy opened."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    launch = BubblewrapBackend().build(
        SandboxPolicy(filesystem=build_filesystem_policy(writable_roots=[str(workspace)])),
        _context(tmp_path),
    )
    argv = list(launch.argv_prefix)
    scratch = str(tmp_path / "tmp")
    windows = [argv[index : index + 2] for index in range(len(argv))]
    assert ["--tmpfs", scratch] in windows
    assert ["--tmpfs", "/tmp"] in windows
    assert not any(
        argv[index] == "--bind" and argv[index + 1] == scratch for index in range(len(argv) - 1)
    )


def test_bwrap_masks_a_denied_directory(tmp_path):
    workspace = tmp_path / "ws"
    secret = workspace / "secret"
    secret.mkdir(parents=True)
    launch = BubblewrapBackend().build(
        SandboxPolicy(
            filesystem=build_filesystem_policy(
                writable_roots=[str(workspace)], denied_paths=[str(secret)]
            )
        ),
        _context(tmp_path),
    )
    argv = list(launch.argv_prefix)
    assert "--remount-ro" in argv
    assert str(secret) in argv


def test_bwrap_prefers_the_system_copy_over_a_bundled_one(tmp_path):
    """The distribution's copy is the one that keeps getting security updates."""
    bundled = tmp_path / "bundled-bwrap"
    bundled.write_text("#!/bin/sh\n", encoding="utf-8")
    bundled.chmod(0o755)

    with patch("core.sandbox.oslayer.bwrap.bundled_bwrap_path", return_value=str(bundled)):
        with patch("core.sandbox.oslayer.bwrap.shutil.which", return_value="/usr/bin/bwrap"):
            assert resolve_bwrap() == "/usr/bin/bwrap"
        # Only when the host has none does the shipped copy come into play.
        with patch("core.sandbox.oslayer.bwrap.shutil.which", return_value=None):
            assert resolve_bwrap() == str(bundled)


def test_bwrap_refuses_when_neither_copy_exists(tmp_path):
    missing = str(tmp_path / "nothing-here")
    with (
        patch("core.sandbox.oslayer.bwrap.shutil.which", return_value=None),
        patch("core.sandbox.oslayer.bwrap.bundled_bwrap_path", return_value=missing),
    ):
        with pytest.raises(SandboxUnavailableError):
            BubblewrapBackend().build(SandboxPolicy(), PolicyContext(cwd="/"))


# ── macOS ───────────────────────────────────────────────────────────────────


def _seatbelt_profile(policy: SandboxPolicy, context: PolicyContext) -> tuple[str, list[str]]:
    with patch.object(SeatbeltBackend, "unavailable_reason", return_value=""):
        launch = SeatbeltBackend().build(policy, context)
    argv = list(launch.argv_prefix)
    return argv[argv.index("-p") + 1], argv


def test_seatbelt_profile_is_closed_by_default(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    profile, argv = _seatbelt_profile(
        SandboxPolicy(
            filesystem=build_filesystem_policy(writable_roots=[str(workspace)]),
            network=NetworkPolicy.RESTRICTED,
        ),
        _context(tmp_path),
    )
    assert "(deny default)" in profile
    # Restricting the network is the absence of a grant, not a deny rule that a
    # later allow could reopen.
    assert "(allow network-outbound)" not in profile
    assert argv[0] == "/usr/bin/sandbox-exec"
    assert argv[-1] == "--"


def test_seatbelt_passes_paths_as_parameters_not_profile_text(tmp_path):
    """A directory name cannot smuggle policy syntax into the profile."""
    workspace = tmp_path / 'we"ird'
    workspace.mkdir()
    profile, argv = _seatbelt_profile(
        SandboxPolicy(filesystem=build_filesystem_policy(writable_roots=[str(workspace)])),
        _context(tmp_path),
    )
    assert str(workspace) not in profile
    assert any(
        argument.startswith("-DWRITABLE_ROOT_") and argument.endswith(f"={workspace}")
        for argument in argv
    )


def test_seatbelt_opens_the_network_only_when_the_policy_does(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    profile, _ = _seatbelt_profile(
        SandboxPolicy(
            filesystem=build_filesystem_policy(writable_roots=[str(workspace)]),
            network=NetworkPolicy.ENABLED,
        ),
        _context(tmp_path),
    )
    assert "(allow network-outbound)" in profile


def test_seatbelt_carves_protected_metadata_out_of_the_write_grant(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    profile, _ = _seatbelt_profile(
        SandboxPolicy(filesystem=build_filesystem_policy(writable_roots=[str(workspace)])),
        _context(tmp_path),
    )
    assert "require-not" in profile
    assert r"\.git" in profile


def _nested_workspace_policy(tmp_path):
    """The desktop layout: the workspace lives inside the denied data directory."""
    state = tmp_path / "data"
    workspace = state / "workspace"
    workspace.mkdir(parents=True)
    (state / "data.db").write_text("secret", encoding="utf-8")
    script = workspace / "script.sh"
    script.write_text("cat script.sh >/dev/null && echo NESTED_OK\n", encoding="utf-8")
    policy = SandboxPolicy(
        filesystem=build_filesystem_policy(writable_roots=[str(workspace)], denied_paths=[str(state)])
    )
    return policy, state, workspace, script


def test_seatbelt_reopens_reads_for_a_workspace_inside_a_denied_directory(tmp_path):
    policy, state, workspace, _ = _nested_workspace_policy(tmp_path)
    _, argv = _seatbelt_profile(policy, _context(tmp_path, cwd=str(workspace)))
    readable = {a.split("=", 1)[1] for a in argv if a.startswith("-DREADABLE_ROOT_") and "_EXCLUDED_" not in a}
    excluded = {a.split("=", 1)[1] for a in argv if a.startswith("-DREADABLE_ROOT_0_EXCLUDED_")}
    assert os.path.realpath(workspace) in readable
    assert os.path.realpath(state) in excluded


@pytest.mark.skipif(
    sys.platform != "darwin" or not os.path.exists("/usr/bin/sandbox-exec"),
    reason="needs macOS sandbox-exec",
)
def test_seatbelt_runs_a_script_in_a_nested_workspace_but_hides_the_data_directory(tmp_path):
    import subprocess

    policy, state, workspace, script = _nested_workspace_policy(tmp_path)
    _, argv = _seatbelt_profile(policy, _context(tmp_path, cwd=str(workspace)))
    ran = subprocess.run([*argv, "/bin/bash", str(script)], cwd=workspace, capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip() == "NESTED_OK"
    leaked = subprocess.run([*argv, "/bin/cat", str(state / "data.db")], cwd=workspace, capture_output=True, text=True)
    assert leaked.returncode != 0 and "secret" not in leaked.stdout


def _capability_store(tmp_path):
    """A capability store laid out like the desktop one, credentials included."""
    caps = tmp_path / "caps"
    builtin = caps / "skills" / "builtin" / "docx" / "rev1"
    plugin = caps / "plugins" / "local" / "visualize" / "rev1"
    plugin_skill = plugin / "package" / "skills" / "visualize"
    view = caps / ".capabilities" / "views" / ("a" * 64) / "skills"
    for directory in (builtin, plugin_skill / "scripts", view):
        directory.mkdir(parents=True)
    (plugin_skill / "scripts" / "render.py").write_text("print('RENDER_OK')\n", encoding="utf-8")
    (plugin / "plugin.json").write_text('{"secret": "plugin-manifest"}', encoding="utf-8")
    (caps / "mcp.json").write_text('{"token": "mcp-secret"}', encoding="utf-8")
    outside = tmp_path / "outside" / "skills"
    outside.mkdir(parents=True)
    escaped = caps / "plugins" / "local" / "evil" / "rev1" / "package"
    escaped.mkdir(parents=True)
    (escaped / "skills").symlink_to(outside, target_is_directory=True)
    return caps, plugin_skill


def test_only_skill_directories_of_the_capability_store_are_reopened(tmp_path, monkeypatch):
    from core.sandbox.permissions import os_gate

    caps, plugin_skill = _capability_store(tmp_path)
    monkeypatch.setenv("HUGAGENT_CAPS_ROOT", str(caps))
    roots = os_gate.capability_skill_roots()
    assert roots == (
        str(caps / "skills"),
        str(plugin_skill.parent),
        str(caps / ".capabilities" / "views" / ("a" * 64) / "skills"),
    )
    for credential in (caps / "mcp.json", caps / "plugins" / "local" / "visualize" / "rev1" / "plugin.json"):
        assert not any(str(credential).startswith(root + os.sep) for root in roots)
    monkeypatch.delenv("HUGAGENT_CAPS_ROOT")
    assert os_gate.capability_skill_roots() == ()


@pytest.mark.skipif(
    sys.platform != "darwin" or not os.path.exists("/usr/bin/sandbox-exec"),
    reason="needs macOS sandbox-exec",
)
def test_seatbelt_lets_a_skill_script_run_while_store_credentials_stay_hidden(tmp_path, monkeypatch):
    import subprocess

    from core.sandbox.permissions import os_gate

    caps, plugin_skill = _capability_store(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("HUGAGENT_CAPS_ROOT", str(caps))
    policy = SandboxPolicy(
        filesystem=build_filesystem_policy(
            writable_roots=[str(workspace)],
            readable_roots=list(os_gate.capability_skill_roots()),
            denied_paths=[str(caps)],
        )
    )
    _, argv = _seatbelt_profile(policy, _context(tmp_path, cwd=str(workspace)))

    def cat(path):
        return subprocess.run([*argv, "/bin/cat", str(path)], cwd=workspace, capture_output=True, text=True)

    assert cat(plugin_skill / "scripts" / "render.py").stdout.strip() == "print('RENDER_OK')"
    for secret in (caps / "mcp.json", caps / "plugins" / "local" / "visualize" / "rev1" / "plugin.json"):
        leaked = cat(secret)
        assert leaked.returncode != 0 and "secret" not in leaked.stdout
    wrote = subprocess.run(
        [*argv, "/bin/sh", "-c", f"echo x > {plugin_skill / 'scripts' / 'render.py'}"],
        cwd=workspace, capture_output=True, text=True,
    )
    assert wrote.returncode != 0, "skill directories are reopened for reading only"


def test_bwrap_binds_readable_roots_back_inside_a_masked_directory(tmp_path):
    state = tmp_path / "data"
    workspace = state / "workspace"
    skills = state / "plugins" / "local" / "visualize" / "rev1" / "package" / "skills"
    workspace.mkdir(parents=True)
    skills.mkdir(parents=True)
    policy = SandboxPolicy(
        filesystem=build_filesystem_policy(
            writable_roots=[str(workspace)], readable_roots=[str(skills)], denied_paths=[str(state)]
        )
    )
    resolved = policy.filesystem.resolve(_context(tmp_path, cwd=str(workspace)))
    args = BubblewrapBackend()._denied_args(resolved, [str(workspace)])
    triples = [args[index : index + 3] for index in range(len(args) - 2)]
    assert ["--bind", str(workspace), str(workspace)] in triples
    assert ["--ro-bind", str(skills), str(skills)] in triples
    assert args.index("--remount-ro") > args.index(str(skills))


# ── Windows ─────────────────────────────────────────────────────────────────


def test_windows_refuses_a_policy_it_cannot_enforce(tmp_path):
    """Read denials and network blocks have no unelevated mechanism; say so."""
    backend = WindowsLowIntegrityBackend()
    context = _context(tmp_path)

    restricted_network = SandboxPolicy(
        filesystem=build_filesystem_policy(writable_roots=[str(tmp_path)]),
        network=NetworkPolicy.RESTRICTED,
    )
    assert "网络" in backend.unenforceable_reason(restricted_network, context)

    narrowed_reads = SandboxPolicy(
        filesystem=build_filesystem_policy(writable_roots=[str(tmp_path)], full_disk_read=False),
        network=NetworkPolicy.ENABLED,
    )
    assert "读取" in backend.unenforceable_reason(narrowed_reads, context)

    with pytest.raises(SandboxUnenforceableError):
        backend.build(restricted_network, context)


def test_windows_produces_a_spawn_plan_rather_than_a_wrapper(tmp_path):
    """A token cannot be an argv prefix, so Windows takes the other launch form."""
    backend = WindowsLowIntegrityBackend()
    policy = SandboxPolicy(
        filesystem=build_filesystem_policy(writable_roots=[str(tmp_path)]),
        network=NetworkPolicy.ENABLED,
    )
    context = _context(tmp_path)
    launch = backend.build(policy, context)

    assert launch.argv_prefix == ()
    assert launch.spawn_plan is not None
    assert launch.spawn_plan["state_dir"] == context.state_dir
    assert str(tmp_path) in [root["path"] for root in launch.spawn_plan["writable_roots"]]
    # The plan survives the JSON hop to the process that will apply it.
    assert SandboxLaunch.from_json(launch.to_json()) == launch


def test_scratch_redirection_does_not_touch_the_spawning_process(tmp_path):
    """A long-lived runner must not rewrite its own TEMP for one command."""
    plan = {"state_dir": str(tmp_path), "scratch_dir": str(tmp_path / "scratch")}
    original = {"PATH": "/usr/bin", "TEMP": "keep-me"}

    child = child_environment(plan, original)

    assert original["TEMP"] == "keep-me"
    for key in TEMP_ENV_KEYS:
        assert child[key] == str(tmp_path / "scratch")
    assert child["PATH"] == "/usr/bin"


def test_a_plan_without_scratch_leaves_the_environment_alone(tmp_path):
    child = child_environment({"state_dir": str(tmp_path)}, {"TEMP": "keep-me"})
    assert child == {"TEMP": "keep-me"}


def test_the_plan_is_decided_before_it_crosses_the_boundary(tmp_path):
    """Resolving on both sides of the boundary would duplicate the decision."""
    workspace = tmp_path / "ws"
    (workspace / ".git").mkdir(parents=True)
    context = _context(tmp_path)
    resolved = build_filesystem_policy(writable_roots=[str(workspace)]).resolve(context)

    plan = build_plan(resolved, context)
    roots = {root["path"]: root["read_only"] for root in plan["writable_roots"]}

    # Carve-outs arrive already worked out; the spawning side never sees a policy.
    assert str(workspace / ".git") in roots[str(workspace)]
    assert "policy" not in plan and "filesystem" not in plan


def test_a_carve_out_nothing_has_created_is_left_out_of_the_plan(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    context = _context(tmp_path)
    resolved = build_filesystem_policy(writable_roots=[str(workspace)]).resolve(context)

    plan = build_plan(resolved, context)

    assert {root["path"]: root["read_only"] for root in plan["writable_roots"]}[
        str(workspace)
    ] == []


def test_labels_follow_the_plan_and_leave_with_it(tmp_path, monkeypatch):
    """A one-off grant must not stay writable for every later command."""
    calls: list[tuple[str, str]] = []
    stub = types.ModuleType("core.sandbox.oslayer.win32.label")
    for name in ("label_writable", "label_read_only", "remove_label"):
        setattr(stub, name, lambda path, _name=name: calls.append((_name, path)))
    monkeypatch.setitem(sys.modules, "core.sandbox.oslayer.win32.label", stub)
    monkeypatch.setattr(windows_runtime.process_state(), 'IN_USE', Counter())
    state = str(tmp_path / "state")
    workspace, once, busy = (str(tmp_path / name) for name in ("ws", "once", "busy"))

    windows_runtime.apply_labels(
        {
            "state_dir": state,
            "writable_roots": [
                {"path": workspace, "read_only": [workspace + "/.git"]},
                {"path": once, "read_only": []},
                {"path": busy, "read_only": []},
            ],
        }
    )
    assert calls == [
        ("label_writable", workspace),
        ("label_read_only", workspace + "/.git"),
        ("label_writable", once),
        ("label_writable", busy),
    ]
    assert LabeledRoots(state).load() == {root_key(p) for p in (workspace, once, busy)}

    calls.clear()
    windows_runtime.process_state().IN_USE[root_key(busy)] += 1
    windows_runtime.apply_labels(
        {"state_dir": state, "writable_roots": [{"path": workspace, "read_only": []}]}
    )

    # The one-off root is unlabeled; the one a running command still holds is not.
    assert calls == [("remove_label", root_key(once)), ("label_writable", workspace)]
    assert LabeledRoots(state).load() == {root_key(workspace), root_key(busy)}


def test_windows_redirects_scratch_space_to_a_private_directory(tmp_path):
    """Granting the user's shared temp folder would be both slow and too broad."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    context = _context(tmp_path)
    resolved = build_filesystem_policy(writable_roots=[str(workspace)]).resolve(context)

    plan = build_plan(resolved, context)
    scratch = scratch_directory(str(context.state_dir))
    roots = [root["path"] for root in plan["writable_roots"]]

    assert plan["scratch_dir"] == scratch
    assert scratch in roots
    assert str(tmp_path / "tmp") not in roots
    assert str(workspace) in roots


def test_a_read_only_plan_asks_for_no_scratch_space(tmp_path):
    context = _context(tmp_path)
    resolved = build_filesystem_policy(writable_roots=[], writable_temp=False).resolve(context)

    plan = build_plan(resolved, context)

    assert plan["writable_roots"] == []
    assert "scratch_dir" not in plan


# ── Selection ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "platform,expected",
    [
        ("macos", "seatbelt"),
        ("linux", "bwrap"),
        ("windows", "windows_low_integrity"),
    ],
)
def test_every_supported_platform_has_a_backend(platform, expected):
    backend = get_platform_backend(platform)
    assert backend is not None and backend.name == expected


def test_an_unknown_platform_raises_rather_than_running_unconfined():
    with pytest.raises(SandboxUnavailableError):
        build_launch(SandboxPolicy(), PolicyContext(cwd=os.sep), platform="plan9")
