"""Build the finite E10/S4 controlled corpus from pinned E06 fixtures."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "services/backend/tests"))

from eval_fixtures import CASES, write_corpus  # noqa: E402


def build(output: Path, *, force: bool = False) -> None:
    output = output.resolve()
    if output.exists() and any(output.iterdir()) and not force:
        raise ValueError("output_exists")
    if output.exists() and force:
        for child in output.iterdir():
            if child.is_dir():
                import shutil

                shutil.rmtree(child)
            else:
                child.unlink()
    output.mkdir(parents=True, exist_ok=True)

    write_corpus(output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        build(args.output, force=args.force)
    except (OSError, ValueError) as exc:
        print(str(exc) if str(exc) in {"output_exists"} else "fixture_build_failed")
        return 2
    print(f"fixtures:ok:{len(CASES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
