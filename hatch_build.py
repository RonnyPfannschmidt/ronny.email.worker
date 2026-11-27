"""Build the interface into the package, for anything that ships.

An editable install is left alone: a service that restarts through ``uv run`` re-installs
itself on the way up, and that must not need npm or a network.  ``npm run watch`` is the
checkout's way.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

BUILT = Path("src/mailmind/web/static/ui/index.html")

#: The image builds the interface in a stage of its own and copies the result in.
PREBUILT = "MAILMIND_UI_PREBUILT"


class InterfaceBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        if version == "editable":
            return
        root = Path(self.root)
        from_sdist = (root / "PKG-INFO").exists()
        if from_sdist or os.environ.get(PREBUILT):
            if not (root / BUILT).exists():
                raise RuntimeError(f"{BUILT} is missing, and this build may not create it")
            return
        npm = shutil.which("npm")
        if npm is None:
            raise RuntimeError("building mailmind from source needs Node and npm")
        for arguments in (["ci", "--ignore-scripts", "--no-fund"], ["run", "build"]):
            subprocess.run([npm, *arguments], cwd=root / "frontend", check=True)
