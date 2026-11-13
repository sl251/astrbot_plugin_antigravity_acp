"""Install the AstrBot logger fixture before importing runtime modules."""

import sys
import types
from unittest.mock import Mock

astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
if not hasattr(api, "logger"):
    api.logger = Mock(spec=["debug", "info", "warning", "error", "exception", "critical"])
astrbot.api = api
