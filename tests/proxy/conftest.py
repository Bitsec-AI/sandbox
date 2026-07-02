import sys
from pathlib import Path


PROXY_DIR = Path(__file__).resolve().parents[2] / "validator" / "proxy"
if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))
