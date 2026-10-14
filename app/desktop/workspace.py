"""Keep default configuration beside the program, independent of launch CWD."""
import sys
from pathlib import Path


def default_workspace(*, source_root=None, executable=None, frozen=None):
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if frozen:
        return Path(executable or sys.executable).resolve().parent
    return Path(source_root or Path(__file__).resolve().parents[2]).resolve()
