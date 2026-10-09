"""Check the pinned local runtime; never print proxy URLs or credential values."""
from importlib.metadata import version
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
from runtime import prepare_environment

if sys.version_info[:2] != (3, 13):
    raise SystemExit("PREFLIGHT_FAILED: this lock is for Python 3.13")
if version("sirchmunk") != "0.1.0":
    raise SystemExit("PREFLIGHT_FAILED: Sirchmunk 0.1.0 is required by the compatibility adapter")
try:
    prepared = prepare_environment(os.environ)
except RuntimeError as error:
    raise SystemExit(str(error))
print(json.dumps({"python": sys.version.split()[0], "sirchmunk": version("sirchmunk"),
                  "rg": "resolved", "rga": "resolved",
                  "proxyNormalizationNeeded": any(prepared.get(key) != os.environ.get(key) for key in ("NO_PROXY", "no_proxy"))}))
