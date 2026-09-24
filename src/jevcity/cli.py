"""Command line entry point. Owner: T6.

jevcity run --scenario PATH [--mode mock|real|replay] [--replay-from CALLS.ndjson]
            [--ticks N] [--agents N] [--out runs/]
jevcity compare RUN_DIR RUN_DIR       # prints final metrics side by side
jevcity export-web RUN_DIR... [--dest web/public/runs]
"""


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError
