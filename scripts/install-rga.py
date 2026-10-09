"""Install pinned official Windows rga archive into project-local tools."""
import hashlib
import io
import json
from pathlib import Path
import urllib.request
import zipfile

base = Path(__file__).resolve().parents[1]
url = "https://github.com/phiresky/ripgrep-all/releases/download/v0.10.9/ripgrep_all-v0.10.9-x86_64-pc-windows-msvc.zip"
data = urllib.request.urlopen(url, timeout=60).read()
destination = base / ".local" / "tools"
destination.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(io.BytesIO(data)) as archive:
    for member in archive.infolist():
        target = (destination / member.filename).resolve()
        if not target.is_relative_to(destination.resolve()):
            raise RuntimeError("Archive path escape")
    archive.extractall(destination)
(base / ".local" / "rga-download.json").write_text(json.dumps({"url": url, "sha256": hashlib.sha256(data).hexdigest()}, indent=2))
print("Installed pinned rga archive; recorded local SHA256 (not an independent signature verification).")
