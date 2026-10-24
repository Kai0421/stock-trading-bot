# Author: Kai0421
"""Run the shadow tick (src/bot/shadowtick.py) using another release's decision layer.

    uv run python scripts/shadow_tick.py --code <release folder> intents --db ... \
        --snapshot ... --out ...

``--code`` puts that release's ``src`` first on the import path, so ``bot.decide`` and
``bot.journal`` are the old release's, while the shadow tick itself is always this checkout's
file (an old release may not have one). Read-only: every broker write is blocked.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]


def main(argv: list[str]) -> int:
    code = HERE
    if argv[:1] == ["--code"]:
        code, argv = Path(argv[1]).resolve(), argv[2:]
    sys.path.insert(0, str(code / "src"))
    spec = importlib.util.spec_from_file_location("shadowtick_impl", HERE / "src/bot/shadowtick.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["shadowtick_impl"] = module
    spec.loader.exec_module(module)
    return module.main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
