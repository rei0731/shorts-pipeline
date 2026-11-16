"""テスト用: config/settings.py が無くても config/settings.example.py を config.settings として読み込む。"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if not (ROOT / "config" / "settings.py").exists():
    spec = importlib.util.spec_from_file_location("config.settings", ROOT / "config" / "settings.example.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["config.settings"] = mod
    spec.loader.exec_module(mod)
