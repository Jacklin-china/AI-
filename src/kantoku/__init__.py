"""Application imports do not generate bytecode throughout the source tree."""

import sys

sys.dont_write_bytecode = True
