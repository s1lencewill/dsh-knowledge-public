"""Production payload executed inside the AppContainer: run the engine, write result.json.

The parent reads `result.json` instead of the child's stdout, so the container needs no inherited
handles. Nothing here holds a credential: with a model pipe configured the engine can only reach the
parent's proxy.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import traceback
import types


def main():
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    state = Path(config["stateDir"])
    state.mkdir(parents=True, exist_ok=True)
    def checkpoint(stage):
        # Closed-set phase only; no traceback, model response, file name or exception message.
        try:
            temporary = state / "entry-progress.tmp"
            temporary.write_text(json.dumps({"stage": stage}), encoding="utf-8")
            temporary.replace(state / "entry-progress.json")
        except OSError:
            pass  # Observation cannot make a job fail.
    checkpoint("BOOTSTRAP")
    # Windows rewrites TEMP during AppContainer startup; restore the authorized state directory.
    for name in ("TEMP", "TMP", "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA"):
        os.environ[name] = str(state)
    # CPython 3.13 asks for a global named pipe for asyncio subprocesses; an AppContainer may only
    # use its own namespace. Scope the adapter to asyncio.windows_utils, never the shared tempfile.
    import asyncio.windows_utils as windows_utils
    original_mktemp = windows_utils.tempfile.mktemp

    def local_pipe_name(**kwargs):
        kwargs["prefix"] = kwargs["prefix"].replace("\\\\.\\pipe\\", "\\\\.\\pipe\\LOCAL\\", 1)
        return original_mktemp(**kwargs)

    windows_utils.tempfile = types.SimpleNamespace(mktemp=local_pipe_name)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    result = {"status": "failed", "answer": "", "evidence": [], "warnings": ["CONTAINER_ENTRY_FAILURE"]}
    try:
        checkpoint("WORKER_IMPORT")
        import worker
        checkpoint("ENGINE_RUN")
        result = asyncio.run(worker.run(config["request"]))
        checkpoint("ENGINE_RETURNED")
    except Exception as error:
        from runtime import safe_error
        (state / "entry-traceback.txt").write_text(traceback.format_exc(), encoding="utf-8")
        # The parent deletes the job directory (and with it the traceback) when the job ends, so an
        # import that the pruned runtime is missing has to be named in the result itself. Import
        # messages are safe ("No module named 'scipy'"); any other class is reported by name only.
        detail = "%s: %s" % (type(error).__name__, error) if isinstance(error, ImportError) \
            else safe_error(error)
        result = {"status": "failed", "answer": "", "evidence": [],
                  "warnings": ["CONTAINER_ENTRY_FAILURE: " + detail]}
    checkpoint("RESULT_SERIALIZE")
    try:
        encoded = json.dumps(result)
    except (TypeError, ValueError, OverflowError):
        # A serialization defect is a failed execution, not an empty or accepted answer.
        encoded = json.dumps({"status":"failed","answer":"","evidence":[],
                              "warnings":["CONTAINER_RESULT_SERIALIZE_FAILED"]})
    checkpoint("RESULT_WRITE")
    (state / "result.json").write_text(encoded, encoding="utf-8")
    checkpoint("RESULT_WRITTEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
