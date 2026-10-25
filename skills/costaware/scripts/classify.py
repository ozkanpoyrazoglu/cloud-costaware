"""Run `costaware classify` from the plugin checkout, without installing the package."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from costaware.cli import main  # noqa: E402

sys.exit(main(["classify", *sys.argv[1:]]))
