"""Run core smoke coverage with a strict no-skip policy."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    """Run the supported-Core smoke module and reject non-executed tests."""
    suite = unittest.defaultTestLoader.discover(str(REPO_ROOT / "tests"), pattern="core_smoke.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    executed = result.testsRun > 0
    strict_success = (
        result.wasSuccessful()
        and not result.skipped
        and not result.expectedFailures
        and not result.unexpectedSuccesses
    )
    return 0 if executed and strict_success else 1


if __name__ == "__main__":
    sys.exit(main())
