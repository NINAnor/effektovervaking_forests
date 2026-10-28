"""`python -m effektovervaking_forests.condition.steps <id> [args...]`."""

import sys

from effektovervaking_forests.condition.steps import BY_ID, run_step

if len(sys.argv) < 2 or sys.argv[1] not in BY_ID:
    sys.exit(f"usage: python -m {__package__} <{'|'.join(BY_ID)}> [args...]")
run_step(sys.argv[1], sys.argv[2:])
