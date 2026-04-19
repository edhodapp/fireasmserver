# pylint: disable=invalid-name
# "__main__" is the conventional Python module name for `python -m`
# entry points. The default module-rgx in the Google pylintrc only
# allows __init__; disabling C0103 here rather than adjusting the
# shared rcfile across every project that pulls it in.
"""Allow `python -m branch_cov ...` invocation."""

import sys

from branch_cov.cli import main

if __name__ == "__main__":
    sys.exit(main())
