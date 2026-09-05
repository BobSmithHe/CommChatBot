"""Keep unit tests independent from a developer's Docker-enabled .env."""
from __future__ import annotations

import os


os.environ.setdefault("SANDBOX_MODE", "local")
