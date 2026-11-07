"""Portable Windows launcher for the WAD Agent Skill bundle."""
import argparse
import sys
from pathlib import Path


def _package_paths():
    package_root = Path(__file__).resolve().parent
    executor_root = (package_root / "runtime" / "executor").resolve()
    try:
        executor_root.relative_to(package_root)
    except ValueError as exc:
        raise RuntimeError("WAD Executor path is outside this Skill package") from exc
    required = (
        package_root / "wad" / "run.py",
        package_root / "wad" / "page_inspection.py",
        executor_root / "executor.py",
    )
    if not all(path.is_file() for path in required):
        raise RuntimeError("WAD runtime files are missing from this Skill package")
    return package_root, executor_root


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the existing Static-only WAD workflow")
    parser.add_argument("domain", help="Hostname only; no scheme, port, path, query, or credentials")
    args = parser.parse_args(argv)
    package_root, executor_root = _package_paths()
    # Keep import paths local to this package and this Python process.
    sys.path.insert(0, str(executor_root))
    sys.path.insert(0, str(package_root))
    from wad.run import initialize
    from wad.page_inspection import inspect_site

    run = initialize(args.domain)
    output_dir = Path.cwd() / "WAD_Output" / run.run_id
    result = inspect_site(run, output_dir)
    print("RUN_ID=" + run.run_id)
    print("STATUS=" + result["status"])
    print("OUTPUT=" + str(output_dir))
    if result["status"] == "FAILED":
        return 1
    if result["status"] not in {"COMPLETE", "PARTIAL"}:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
