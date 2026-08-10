# Created by Odoo — Technical Support tooling (scrubdb).
"""``python -m scrubdb`` entrypoint."""
import sys

from .cli import run

if __name__ == "__main__":
    sys.exit(run())
