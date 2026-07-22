"""Focused tests for the agent-skill SKILL.md parser."""

from core.agent_skills.registry import _load_skill_metadata_from_str, _split_frontmatter


def test_frontmatter_accepts_windows_crlf_line_endings():
    raw = (
        "---\r\n"
        "name: windows-skill\r\n"
        "description: A skill packaged on Windows.\r\n"
        "version: 1.2.3\r\n"
        "---\r\n"
        "\r\n"
        "# Windows skill\r\n"
        "\r\n"
        "Follow the instructions.\r\n"
    )

    frontmatter, body = _split_frontmatter(raw)
    metadata = _load_skill_metadata_from_str(raw, "windows-skill")

    assert frontmatter["name"] == "windows-skill"
    assert body.startswith("\n# Windows skill\n")
    assert metadata.description == "A skill packaged on Windows."
    assert metadata.version == "1.2.3"
