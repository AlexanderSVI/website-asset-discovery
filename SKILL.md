---
name: website-asset-discovery
description: Run Website Asset Discovery (WAD) on one public website with the existing Static-only workflow.
compatibility: Windows, CPython 3.11 or later, and WAD runtime dependencies installed in the Skill environment.
---

# WAD — Website Asset Discovery

WAD is an Agent Skill for a bounded, Static-only survey of one public website. It does not use a browser, execute website JavaScript, submit forms, or test business functions.

## Resolve the installed Skill directory

The repository root is the Skill directory. It contains this `SKILL.md`, `run_wad.py`, and all WAD runtime files.

- In **Codex**, use the active Skill's `path` value from the Skill metadata. The Skill directory is the parent of that `SKILL.md`.
- In **Claude Code**, `${CLAUDE_SKILL_DIR}` resolves to the directory containing this Skill file.

Do not find the WAD runtime through the shell's current directory or an absolute developer path. If the Skill directory cannot be resolved, stop and report that problem.

## Python setup

The current WAD Executor uses the Windows Job Object API. Use Windows with CPython 3.11 or later. Codex Desktop on Windows is the development target; Claude Code uses the same Skill format, but WAD execution in Claude Code has not been verified. macOS and Linux runtimes are unsupported by the current Executor.

During installation, create `.venv` in the Skill directory and install `requirements-runtime.txt`. If `.venv` or HTTPX is missing when a run is requested, give the user the setup instructions in `README.md`; do not install packages during a WAD survey.

## Run WAD

Ask for the website domain only when it is missing. Request a hostname with no scheme, port, path, query, or credentials. Pass it to the launcher as a separate argument. Use the Python interpreter inside the Skill's `.venv`.

The launcher resolves its runtime relative to its own file and modifies `sys.path` only in its own Python process. It calls the existing `wad.run.initialize(domain)` and `wad.page_inspection.inspect_site(run, output_dir)` once. It writes `WAD_Output/<run_id>/` under the user's current project directory, so start the agent in the project where the result should be saved.

Preserve the existing HTTP, time, page, URL, Sitemap, byte, host, and robots limits. Do not retry outside WAD. Do not start browser inspection, submit forms, or change website data. If WAD returns `PARTIAL`, keep the available output and report its recorded reason.

## Report the result

Use the exact `RUN_ID`, `STATUS`, and `OUTPUT` printed by the launcher. Read `Discovery_Report.md` and identify which standard files exist:

- `Discovery_Report.md`
- `data/Asset_Inventory.csv`
- `data/Occurrences.csv`
- `data/Sources.csv`
- `data/Evidence.csv`
- `data/Relationships.csv`

Report `COMPLETE`, `PARTIAL`, or `FAILED` exactly as returned and recorded. Do not claim complete website coverage or infer that uninspected resources are absent. See `docs/RESULT_INTERPRETATION_GUIDE.md` for CSV and status interpretation.