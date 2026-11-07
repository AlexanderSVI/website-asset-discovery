# WAD v0.1.0 — Website Asset Discovery

WAD is an installable Agent Skill for creating a bounded, static inventory of a website before it is modernized. It helps answer: **What was found on the website, where is it used, and what evidence supports the result?**

WAD treats an asset as a discoverable website element that may matter to search engine indexing, visitors, site content, or site functionality. Examples include pages, content blocks, images, documents, navigation, and some functional elements.

WAD records supported assets, where they appear, their information sources, observations, and relationships. The results can provide an evidence-based starting point for preparing a specification for a new website. WAD helps discover and describe assets; it does not decide which assets to keep or define the new website.

## Future directions

The project plans to develop a method for assessing discovered assets, followed by recommendations for migrating suitable assets to a modernized website. These are provisional future directions, not WAD v0.1.0 features. The current version gathers source information but does not assess asset value or decide what to migrate.

## What WAD examines

WAD performs a bounded, static inspection of one website. Depending on the content received and the current implementation, results may include:

- HTML pages and content blocks;
- images and documents referenced by inspected pages;
- navigation and links between discovered pages;
- static indicators of components such as forms and search.

WAD links records to their sources and supporting observations. A discovered link does not necessarily mean that WAD fetched or inspected the linked resource.

WAD does not run website JavaScript, inspect websites in a browser, submit forms, test business workflows, or access authenticated areas. It does not guarantee that it will find every asset. `COMPLETE` means that the applicable stages of that run finished; it does not prove that the inventory is exhaustive. `PARTIAL` means that a restriction or blocker affected the results. Review the report and unchecked sources before relying on the inventory.

For details about statuses, CSV records, relationships, evidence, and coverage, see the [WAD Result Interpretation Guide](docs/RESULT_INTERPRETATION_GUIDE.md). The [Responsible Crawling Policy](RESPONSIBLE_CRAWLING_POLICY.md) describes WAD’s rules for handling data from third-party websites.

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

After publication, replace `<WAD_GITHUB_URL>` with the repository URL. Clone the repository root into your user-level Skills directory to make WAD available across projects.

### Codex on Windows

```powershell
$skillHome = Join-Path $HOME '.agents\skills'
New-Item -ItemType Directory -Force -Path $skillHome | Out-Null
git clone '<WAD_GITHUB_URL>' (Join-Path $skillHome 'website-asset-discovery')
```

Codex searches the user-level `~/.agents/skills/` directory. The cloned directory must contain `SKILL.md` at its root. Invoke it with `$website-asset-discovery` or ask Codex to use the WAD Skill.

To ask Codex Desktop to install WAD from GitHub, replace the placeholder with the published repository URL and send Codex this prompt:

```text
Install the WAD Skill from <WAD_GITHUB_URL> in my user-level Codex Skills directory: ~/.agents/skills/website-asset-discovery. Verify that the directory contains SKILL.md, the launcher, and the runtime files. Follow the README to create an isolated Python environment and install the runtime dependencies. Do not modify the current project. After installation, check whether Codex Desktop can discover the Skill. Report Skill discovery and WAD runtime verification separately. Do not say that runtime execution was verified unless you ran it. If you lack access or required information, stop and explain what is missing.
```

You may need to start a new Codex session before the Skill appears.

### Claude Code on Windows

```powershell
$skillHome = Join-Path $HOME '.claude\skills'
New-Item -ItemType Directory -Force -Path $skillHome | Out-Null
git clone '<WAD_GITHUB_URL>' (Join-Path $skillHome 'website-asset-discovery')
```

Claude Code discovers user-level Skills from `~/.claude/skills/<skill-name>/SKILL.md`. If the Skill does not appear, start a new Claude Code session. Invoke it as `/website-asset-discovery`.

These commands install the Skill folder. They do not create a GitHub repository or publish this local staging copy.

## Python requirements

WAD’s Executor uses the Windows Job Object API. Use Windows and CPython 3.11 or later. Runtime execution has been verified in Codex Desktop on Windows. WAD runtime execution in Claude Code has not been verified. The current Executor does not support macOS or Linux.

Create an isolated Python environment inside the installed Skill directory and install the runtime dependency:

```powershell
$skillRoot = Join-Path $HOME '.agents\skills\website-asset-discovery'
python -m venv (Join-Path $skillRoot '.venv')
& (Join-Path $skillRoot '.venv\Scripts\python.exe') -m pip install -r (Join-Path $skillRoot 'requirements-runtime.txt')
```

For Claude Code, set `$skillRoot` to `$HOME\.claude\skills\website-asset-discovery`. The optional `requirements-test.txt` installs the test-only certificate helper dependency. WAD source files and third-party packages are installed separately; third-party source is not vendored.

## Dependencies and licenses

WAD source files are licensed under the MIT License in `LICENSE`. The listed copyright holder is Alexander Svistelnikov. Runtime dependency HTTPX 0.28.1 is licensed under BSD-3-Clause. The optional test dependency cryptography 46.0.7 is available under Apache-2.0 or BSD-3-Clause. Each dependency remains under its own license; third-party source is not included in this repository. HTTPX declares transitive dependencies, which pip resolves from package metadata. This release does not include a lock file.

- [HTTPX license](https://github.com/encode/httpx/blob/0.28.1/LICENSE.md)
- [cryptography license](https://github.com/pyca/cryptography/blob/46.0.7/LICENSE)

## Questions and feedback

For questions or suggestions about WAD, email [alex.svist@yahoo.com](mailto:alex.svist@yahoo.com).

## For developers: run the WAD tests

This section is for people developing the package. It is not needed for normal WAD use. The tests use local HTTPS loopback fixtures and do not contact public websites. First, install `requirements-test.txt` in the Skill virtual environment. From PowerShell, run the test suite with a process-local Python path for the Executor test helper:

```powershell
$skillRoot = Join-Path $HOME '.agents\skills\website-asset-discovery'
$python = Join-Path $skillRoot '.venv\Scripts\python.exe'
$env:PYTHONPATH = "$skillRoot;$skillRoot\runtime\executor"
& $python -B -m unittest discover -s (Join-Path $skillRoot 'wad') -p 'test_*.py' -v
Remove-Item Env:PYTHONPATH
```

Use this command only for tests. Normal WAD runs use the in-package launcher and do not require manual `PYTHONPATH` configuration. The test suite has not yet been run from a clean copy of this public Skill package.
