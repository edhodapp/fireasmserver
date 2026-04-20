# pylint: disable=invalid-name
# "__main__" is the conventional Python module name for `python -m`
# entry points. The default module-rgx in the Google pylintrc only
# allows __init__; disabling C0103 here rather than adjusting the
# shared rcfile across every project that pulls it in. Matches the
# pattern used in tooling/src/branch_cov/__main__.py.
"""Allow ``python -m audit_ontology ...`` invocation."""

import sys

from audit_ontology.cli import main

if __name__ == "__main__":
    sys.exit(main())
