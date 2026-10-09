"""Execute installed Sirchmunk's real filename mode without a model or user data.

Goes through the trusted parent pipeline: the snapshot is built in this process and the engine
only ever receives `snapshotDir`, exactly like the service path.
"""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

base = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(base / "engine"))
from runtime import child_environment, prepare_environment
import pipeline

try:
    env = child_environment(prepare_environment(os.environ))
except RuntimeError as error:
    raise SystemExit(str(error))

cache = base / ".local/query-cache"
work = cache / ("probe-" + uuid.uuid4().hex[:8])
request = {"mode": "FILENAME_ONLY", "query": "note-03", "root": str(base / ".local/fixtures"),
           "libraryId": "fixtures", "cacheRoot": str(cache), "workDir": str(work)}
captured = {"stdout": "", "stderr": ""}


async def runner(child_request):
    result = subprocess.run([sys.executable, str(base / "engine/worker.py")],
                            input=json.dumps(child_request), text=True, capture_output=True,
                            env=env, timeout=60)
    captured["stdout"], captured["stderr"] = result.stdout or "", result.stderr or ""
    marker = "DSH_KNOWLEDGE_RESULT="
    return next((json.loads(line[len(marker):]) for line in captured["stdout"].splitlines()
                 if line.startswith(marker)), None)


try:
    payload = asyncio.run(pipeline.run_local(request, runner))
finally:
    shutil.rmtree(work, ignore_errors=True)

if not payload or payload["status"] == "failed":
    (base / ".local/engine-probe-stderr.log").write_text(captured["stderr"], encoding="utf-8")
    raise SystemExit("Engine probe failed: " + json.dumps(payload))
if "note-03.md" not in [match["name"] for match in payload.get("matches", [])]:
    raise SystemExit("Engine probe failed: expected note-03.md was not returned")
if "Failed to sync to parquet" in captured["stderr"] or "Final parquet shutdown sync failed" in captured["stderr"]:
    raise SystemExit("Engine probe failed: storage shutdown error")
report = {"sirchmunk": "0.1.0", "mode": "FILENAME_ONLY", "result": payload,
          "intelligentSearchValidated": False}
(base / "docs/engine-filename-probe.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
print(json.dumps(report, ensure_ascii=True))
