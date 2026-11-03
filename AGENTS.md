# Repository instructions

`openloggernet` is an open replacement for parts of Campbell Scientific's LoggerNet: a PakBus/BMP5
client, a compiler runner and the `oln` command line. It is published to PyPI, and the
[open-crbasic VS Code extension](https://github.com/UtrechtUniversity/open-crbasic-vsix) drives
loggers through `oln --json`.

Priorities: correctness, evidence, simplicity, minimal scope, maintainability.

## Layout

| Path | Contents |
|---|---|
| `src/openloggernet/` | The package: `cli` (the `oln` command), `pakbus` (the PakBus client), `cr6comp`, and modules for the larger commands. |
| `tests/` | Host tests; no logger needed. |
| `docs/pakbus.md` | Wire format and what has been confirmed on hardware. |
| `docs/lessons.md` | Logger behaviour the code depends on, with evidence. |

The package must not assume the layout of any other repository. Campbell's compilers, help files
and manuals are proprietary and are never committed here.

## Evidence

Never report a test, flash or hardware result that was not actually observed. Label inference and
unverified behaviour explicitly. A new wire-format fact goes in `docs/pakbus.md` with how it was
confirmed; a new logger behaviour goes in `docs/lessons.md` with its evidence, after showing the
evidence to the human.

## Hardware

Agents may talk to a confirmed bench logger. Sending a changed program deletes the replaced
program's data tables, so preserve needed data first. Never flash or set values on a field logger
without explicit human approval.

## Checks

```bash
uv sync
uv run pre-commit run --all-files   # ruff lint and format, pyrefly, file hygiene
uv run pytest
```

Run the narrowest useful test right after an edit and the full set before completion. CI runs the
same commands. Add or remove dependencies with `uv add`/`uv remove`.

## Releases

`main` publishes to PyPI when `version` in `pyproject.toml` is new; `dev` publishes every push to
TestPyPI. Bump the version only when the human asks for a release.

## Version control

Never create a merge commit; integrate with rebase. Commit only when asked. Do not use destructive
reset or checkout commands without explicit approval.
