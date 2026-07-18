"""PowerPoint (.pptx) engine for the ``ppt-design`` skill. The CLI in
``scripts/ppt.py`` is the only intended entry point; do not import this
package from outside the skill.

Submodules (builder / editor / reader / qa / thumbnails / themes / …) are
imported on demand by callers — keeping cheap subcommands of the CLI
(``list-themes`` etc.) from paying the python-pptx + lxml import cost on
every invocation.
"""

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
