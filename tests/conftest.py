import json
import pathlib
import sys
import types
from pathlib import Path

import pytest

# Stub haffmpeg so HA's built-in ffmpeg component can load in the test
# environment where the real haffmpeg C-ext package is unavailable.
if "haffmpeg" not in sys.modules:
    _haffmpeg = types.ModuleType("haffmpeg")
    sys.modules["haffmpeg"] = _haffmpeg

    _haffmpeg_core = types.ModuleType("haffmpeg.core")

    class _HAFFmpeg:
        def __init__(self, *a, **kw): pass

    _haffmpeg_core.HAFFmpeg = _HAFFmpeg  # type: ignore[attr-defined]
    sys.modules["haffmpeg.core"] = _haffmpeg_core

    _haffmpeg_tools = types.ModuleType("haffmpeg.tools")
    _haffmpeg_tools.IMAGE_JPEG = "image/jpeg"  # type: ignore[attr-defined]

    class _FFVersion:
        def __init__(self, *a, **kw): pass
        async def get_version(self): return None

    _haffmpeg_tools.FFVersion = _FFVersion  # type: ignore[attr-defined]

    class _ImageFrame:
        def __init__(self, *a, **kw): pass

    _haffmpeg_tools.ImageFrame = _ImageFrame  # type: ignore[attr-defined]
    sys.modules["haffmpeg.tools"] = _haffmpeg_tools

FIXTURES = Path(__file__).parent / "fixtures"

# Ensure custom_components.__path__ only contains real filesystem paths (not editable-install
# placeholders). The editable-install namespace hook adds a fake sentinel path that HA's
# component scanner tries to iterate as a directory, causing FileNotFoundError.
_CC_ROOT = str(pathlib.Path(__file__).parent.parent / "custom_components")
try:
    import custom_components as _cc  # noqa: PLC0415
    _cc.__path__ = [p for p in list(_cc.__path__) if pathlib.Path(p).exists()]
    if _CC_ROOT not in _cc.__path__:
        _cc.__path__.insert(0, _CC_ROOT)
except ImportError:
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))


@pytest.fixture
def load_fixture():
    def _load(name: str):
        return json.loads((FIXTURES / name).read_text())
    return _load


pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations) -> None:
    """Automatically enable custom integrations for all tests."""
    return None
