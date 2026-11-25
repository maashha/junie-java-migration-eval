#!/usr/bin/env python3
"""Run ONE OpenRewrite (UpgradeToJava17) migration per project and evaluate it.

    python scripts/run_openrewrite.py -p tinify-java
    python scripts/run_openrewrite.py -p tinify-java --dry-run

Same options as run_junie.py. Requires a valid baseline (see run_experiment.py).
"""

from __future__ import annotations

import sys

from run_junie import main

if __name__ == "__main__":
    sys.exit(main("openrewrite"))
