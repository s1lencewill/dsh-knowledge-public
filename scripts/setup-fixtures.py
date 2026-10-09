"""Create only synthetic, disposable data under .local; never import user documents."""
import json
from pathlib import Path

base = Path(__file__).resolve().parents[1]
root = base / ".local" / "fixtures"
root.mkdir(parents=True, exist_ok=True)
for number in range(20):
    folder = root / ("中文资料" if number % 2 else "with spaces")
    folder.mkdir(exist_ok=True)
    (folder / f"note-{number:02}.md").write_text(
        f"# Synthetic fixture {number}\n\nThe project codename is WALNUT-{number:02}.\n检索测试：第 {number} 号资料。\n",
        encoding="utf-8",
    )
(root / ".env").write_text("SYNTHETIC_SECRET=must-not-be-copied\n", encoding="utf-8")
config = {"libraries": [{"id": "fixtures", "name": "M0 synthetic fixtures", "workspace": str(base),
                          "root": str(root), "syntheticFixture": True}]}
(base / ".local" / "libraries.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
print("Created 20 synthetic documents and one excluded .env fixture.")
