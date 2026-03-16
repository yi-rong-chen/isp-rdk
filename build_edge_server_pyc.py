#!/usr/bin/env python3
"""Compile edge-server Python sources to sourceless .pyc files."""

from __future__ import annotations

import argparse
import os
import py_compile
import shutil
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    default_root = script_dir

    parser = argparse.ArgumentParser(
        description="Compile edge-server sources to same-directory .pyc files and remove .py files."
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=str(default_root),
        help="Target directory to process. Defaults to ./",
    )
    parser.add_argument(
        "--keep-source",
        action="store_true",
        help="Keep the original .py files after compilation.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print planned actions without writing files.",
    )
    parser.add_argument(
        "--include-self",
        action="store_true",
        help="Allow processing this script when it is inside the target directory.",
    )
    return parser.parse_args()


def should_skip(path: Path, script_path: Path, include_self: bool) -> bool:
    if not include_self and path.resolve() == script_path:
        return True

    skip_dirs = {"__pycache__", ".git", ".idea", ".pytest_cache", ".mypy_cache", "venv", ".venv"}
    if any(part in skip_dirs for part in path.parts):
        return True

    # Ignore macOS AppleDouble metadata files copied into Linux deployments.
    return path.name.startswith("._") or path.name.startswith(".__")


def collect_sources(root: Path, script_path: Path, include_self: bool) -> list[Path]:
    sources: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        if should_skip(path, script_path, include_self):
            continue
        sources.append(path)
    return sources


def compile_sources(root: Path, sources: list[Path], dry_run: bool) -> tuple[list[Path], list[str]]:
    compiled: list[Path] = []
    errors: list[str] = []

    for src in sources:
        dst = src.with_suffix(".pyc")
        display = src.relative_to(root)
        if dry_run:
            print(f"[DRY-RUN] compile {display} -> {dst.relative_to(root)}")
            continue

        try:
            py_compile.compile(
                str(src),
                cfile=str(dst),
                dfile=str(display),
                doraise=True,
            )
            compiled.append(dst)
            print(f"[OK] compiled {display} -> {dst.relative_to(root)}")
        except py_compile.PyCompileError as exc:
            errors.append(f"{display}: {exc.msg}")
        except Exception as exc:  # pragma: no cover - defensive path
            errors.append(f"{display}: {exc}")

    return compiled, errors


def delete_sources(root: Path, sources: list[Path], dry_run: bool) -> None:
    for src in sources:
        display = src.relative_to(root)
        if dry_run:
            print(f"[DRY-RUN] delete  {display}")
            continue
        src.unlink()
        print(f"[OK] deleted  {display}")


def cleanup_pycache(root: Path, dry_run: bool) -> None:
    for cache_dir in sorted(root.rglob("__pycache__")):
        if not cache_dir.is_dir():
            continue
        display = cache_dir.relative_to(root)
        if dry_run:
            print(f"[DRY-RUN] remove  {display}/")
            continue
        shutil.rmtree(cache_dir)
        print(f"[OK] removed  {display}/")


def cleanup_metadata_files(root: Path, dry_run: bool) -> None:
    for pattern in ("._*.py", ".__*.py"):
        for metadata_file in sorted(root.rglob(pattern)):
            if not metadata_file.is_file():
                continue
            display = metadata_file.relative_to(root)
            if dry_run:
                print(f"[DRY-RUN] remove  {display}")
                continue
            metadata_file.unlink()
            print(f"[OK] removed  {display}")


def rollback_compiled_files(compiled: list[Path]) -> None:
    for path in compiled:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass


def main() -> int:
    args = parse_args()
    script_path = Path(__file__).resolve()
    root = Path(args.root).resolve()

    if not root.exists():
        print(f"Target directory does not exist: {root}", file=sys.stderr)
        return 1
    if not root.is_dir():
        print(f"Target path is not a directory: {root}", file=sys.stderr)
        return 1

    sources = collect_sources(root, script_path, args.include_self)
    if not sources:
        print(f"No Python files found under {root}")
        return 0

    print(f"Target directory: {root}")
    print(f"Python files found: {len(sources)}")
    print(f"Delete source files: {'no' if args.keep_source else 'yes'}")
    print(f"Dry-run mode: {'yes' if args.dry_run else 'no'}")

    compiled, errors = compile_sources(root, sources, args.dry_run)
    if errors:
        print("\nCompilation failed. Source files were kept.", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        rollback_compiled_files(compiled)
        return 1

    if not args.keep_source:
        delete_sources(root, sources, args.dry_run)

    cleanup_pycache(root, args.dry_run)
    cleanup_metadata_files(root, args.dry_run)

    compiled_count = len(sources) if args.dry_run else len(compiled)
    print(
        f"\nCompleted successfully: {compiled_count} files compiled"
        f"{'' if args.keep_source else ', source files removed'}."
    )
    return 0


if __name__ == "__main__":
    os.umask(0o022)
    raise SystemExit(main())
