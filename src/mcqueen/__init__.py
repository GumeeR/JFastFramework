"""Terminal character helpers exposed as ``import mcqueen``."""

from __future__ import annotations

import sys
from typing import TextIO

ART = r"""\
              _________
       ______/   95    \______
   ___/  _                 _  \___
  /_____/ \_______________/ \_____\
      (___)               (___)
"""


def show(stream: TextIO | None = None) -> None:
    """Print the bundled race-car art to the selected terminal stream."""
    print(ART, end="", file=stream or sys.stdout)


__all__ = ["ART", "show"]
