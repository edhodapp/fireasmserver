# pylint: disable=invalid-name
"""Allow `python -m req_coverage` invocation alongside the
console-script entry point. Module name `__main__` is a Python
convention for runnable modules; the C0103 disable above is
unavoidable here."""

from __future__ import annotations

import sys

from req_coverage.cli import main

if __name__ == "__main__":
    sys.exit(main())
