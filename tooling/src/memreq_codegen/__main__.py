# pylint: disable=invalid-name
# "__main__" is the conventional Python module name for `python -m`
# entry points. Same disable pattern as discipline/__main__.py.
"""Allow `python -m memreq_codegen ...` invocation."""

from __future__ import annotations

import sys

from memreq_codegen.cli import main

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
