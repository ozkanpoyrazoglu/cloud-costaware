"""PreToolUse hook entry point; runs costaware from the plugin checkout.

Usage: python3 hooks/pretooluse.py [--client claude|antigravity|codex]
The client is detected from the payload when --client is omitted.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from costaware.hook import main  # noqa: E402

client = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--client" else None
sys.exit(main(client))
