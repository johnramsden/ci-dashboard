"""`python -m ci_dashboard` is the `ci-dashboard` command."""

import sys

from .cli import main

sys.exit(main())
