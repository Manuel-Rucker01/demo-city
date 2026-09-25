"""Timing budgets for performance tests. CI runners are slower and noisier than a laptop, so
budgets are multiplied by PERF_SLACK there (override with JEVCITY_PERF_SLACK)."""

import os

PERF_SLACK = float(os.environ.get("JEVCITY_PERF_SLACK", "5" if os.environ.get("CI") else "1"))
