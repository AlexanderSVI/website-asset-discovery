# WAD v0.1.1 — Website Asset Discovery

**Runtime support: Windows only.** WAD is an installable Agent Skill for creating a bounded, static inventory of an existing website before modernization. The current Executor uses the Windows Job Object API and does not run on macOS or Linux.

WAD helps answer: **What was found on the website, where is it used, and what evidence supports the result?** It treats an asset as a discoverable website element that may matter to search-engine indexing, visitors, site content, or site functionality. Examples include pages, content blocks, images, documents, navigation, and some functional components.

WAD records supported assets, where they appear, their information sources, observations, and relationships. The results can provide an evidence-based starting point for preparing a specification for a new website. WAD helps discover and describe assets; it does not decide which assets to keep or define the new website.

## Future directions

The project plans to develop a method for assessing discovered assets, followed by recommendations for migrating suitable assets to a modernized website. These are future directions, not features of this release. WAD gathers source information but does not assess asset value or decide what to migrate.

## What WAD examines

WAD performs a bounded, static inspection of one website. Depending on the content received and the current implementation, results may include:

- HTML pages and content blocks;
- images and documents referenced by inspected pages;
- navigation and links between discovered pages;
- static indicators of components such as forms and search.

WAD links records to their sources and supporting observations. A discovered link does not necessarily mean that WAD fetched or inspected the linked resource.

WAD does not run website JavaScript, inspect pages in a browser, submit forms, test business workflows, or access authenticated areas. It does not guarantee that it will find every asset. `COMPLETE` means that the applicable stages of a run finished; it does not prove that the inventory is exhaustive. `PARTIAL` means a restriction or blocker affected the results. Review the report and unchecked sources before relying on the inventory.

For details about statuses, CSV records, relationships, evidence, and coverage, see the [WAD Result Interpretation Guide](docs/RESULT_INTERPRETATION_GUIDE.md). For an explicitly synthetic example of an owner-facing report, see the [WAD Interpretation Report Example](docs/WAD_INTERPRETATION_REPORT_EXAMPLE.md). To prepare a report from a real run, see the [WAD Interpretation Report Prompt](docs/WAD_INTERPRETATION_REPORT_PROMPT.md). The [Responsible Crawling Policy](RESPONSIBLE_CRAWLING_POLICY.md) describes WAD’s rules for handling data from third-party websites.

## Quick start

1. Install the Skill in Codex or Claude Code using the instructions below.
2. Create an isolated Python environment in the installed Skill directory and install the runtime dependencies.
3. Open the website project where you want to save the results in Codex or Claude Code. Invoke WAD and provide the hostname: `Use the WAD Skill to inspect <HOSTNAME>`.
4. Open `WAD_Output/<run_id>/Discovery_Report.md`. Review the run status, limits, and uninspected sources before reviewing the related CSV files.

Provide only a hostname. Do not include `https://`, a path, query parameters, or credentials.

## Run results

Each run writes a package to `WAD_Output/<run_id>/` in the current website project. The package contains `Discovery_Report.md` and a `data/` directory with five CSV files:

- `Asset_Inventory.csv` — records for distinct assets;
- `Occurrences.csv` — recorded placements or uses of assets;
- `Sources.csv` — information sources and their inspection status;
- `Evidence.csv` — observations that support records;
- `Relationships.csv` — typed connections between records or URLs.

The report summarizes the run status, counts, limits, and recorded reasons for incomplete inspection. Read the report first, then review the CSV files. Report and CSV counts describe different entities; do not add them together. The structured report and CSV files are also convenient for review by AI agents such as Codex. AI-assisted review does not replace checking the records and supporting observations.

## Install the Skill from GitHub

Clone the repository root into your user-level Skills directory to make WAD available across projects.

### Codex on Windows

```powershell
$skillHome = Join-Path $HOME '.agents\skills'
New-Item -ItemType Directory -Force -Path $skillHome | Out-Null
git clone 'https://github.com/AlexanderSVI/website-asset-discovery.git' (Join-Path $skillHome 'website-asset-discovery')
```

Codex searches the user-level `~/.agents/skills/` directory. The cloned directory must contain `SKILL.md` at its root. Invoke the Skill with `$website-asset-discovery` or ask Codex to use WAD.

To ask Codex Desktop to install WAD, send it this prompt:

```text
Install the WAD Skill from https://github.com/AlexanderSVI/website-asset-discovery.git in my user-level Codex Skills directory: ~/.agents/skills/website-asset-discovery. Verify that the directory contains SKILL.md, the launcher, and the runtime files. Follow the README to create an isolated Python environment and install the runtime dependencies. Do not modify the current project. After installation, check whether Codex Desktop can discover the Skill. Report Skill discovery and WAD runtime verification separately. Do not say that runtime execution was verified unless you ran it. If you lack access or required information, stop and explain what is missing.
```

You may need to start a new Codex session before the Skill appears.

### Claude Code on Windows

```powershell
$skillHome = Join-Path $HOME '.claude\skills'
New-Item -ItemType Directory -Force -Path $skillHome | Out-Null
git clone 'https://github.com/AlexanderSVI/website-asset-discovery.git' (Join-Path $skillHome 'website-asset-discovery')
```

Claude Code discovers user-level Skills from `~/.claude/skills/<skill-name>/SKILL.md`. If the Skill does not appear, start a new Claude Code session. Invoke it as `/website-asset-discovery`.

The Skill was installed and discovered on macOS in checks by two users. Those checks did not run WAD. WAD runtime execution on macOS is unsupported by the current Windows-only Executor.

## Python requirements

Use Windows and CPython 3.11 or later. WAD runtime execution has been verified in Codex Desktop on Windows. Runtime execution in Claude Code has not been verified. macOS and Linux runtimes are unsupported by the current Executor.

Create an isolated Python environment inside the installed Skill directory and install the runtime dependency:

```powershell
$skillRoot = Join-Path $HOME '.agents\skills\website-asset-discovery'
python -m venv (Join-Path $skillRoot '.venv')
& (Join-Path $skillRoot '.venv\Scripts\python.exe') -m pip install -r (Join-Path $skillRoot 'requirements-runtime.txt')
```

For Claude Code on Windows, set `$skillRoot` to `$HOME\.claude\skills\website-asset-discovery`. The optional `requirements-test.txt` installs the test-only certificate helper dependency. WAD source files and third-party packages are installed separately; third-party source is not vendored.

## Verification

The public Skill package was installed on a separate Windows computer. On Windows with CPython 3.13.14 and HTTPX 0.28.1, the WAD test suite passed 86 tests and the Executor test suite passed 31 tests. After the TLS 1.2 test-fixture update, the five affected HTTPS fixture tests passed. These tests use local fixtures and do not survey public websites.

The macOS checks confirmed Skill installation and discovery only. They did not verify WAD runtime execution.

## Dependencies and licenses

WAD source files are licensed under the MIT License in `LICENSE`. The listed copyright holder is Alexander Svistelnikov. Runtime dependency HTTPX 0.28.1 is licensed under BSD-3-Clause. The optional test dependency cryptography 46.0.7 is available under Apache-2.0 or BSD-3-Clause. Each dependency remains under its own license; third-party source is not included in this repository. HTTPX declares transitive dependencies, which pip resolves from package metadata. This release does not include a lock file.

- [HTTPX license](https://github.com/encode/httpx/blob/0.28.1/LICENSE.md)
- [cryptography license](https://github.com/pyca/cryptography/blob/46.0.7/LICENSE)

## Questions and feedback

For questions or suggestions about WAD, email [alex.svist@yahoo.com](mailto:alex.svist@yahoo.com).

## For developers: run the WAD tests

This section is for developers; normal WAD use does not require running tests. The tests use local HTTPS loopback fixtures and do not contact public websites. First, install `requirements-test.txt` in the Skill virtual environment. From PowerShell, run both test suites with a process-local Python path for the Executor test helper:

```powershell
$skillRoot = Join-Path $HOME '.agents\skills\website-asset-discovery'
$python = Join-Path $skillRoot '.venv\Scripts\python.exe'
$previousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = "$skillRoot;$skillRoot\runtime\executor"
    & $python -B -m unittest discover -s (Join-Path $skillRoot 'wad') -p 'test_*.py' -v
    if ($LASTEXITCODE -ne 0) { throw 'WAD tests failed.' }
    & $python -B -m unittest discover -s (Join-Path $skillRoot 'runtime\executor') -p 'test_executor.py' -v
    if ($LASTEXITCODE -ne 0) { throw 'Executor tests failed.' }
}
finally {
    if ($null -eq $previousPythonPath) {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONPATH = $previousPythonPath
    }
}
```

The test results above were obtained from the public package sources on Windows. They do not claim that the full test suites were run on the separate installation computer.