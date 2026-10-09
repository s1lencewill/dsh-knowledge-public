"""Synthetic isolation probe payload; copied beside worker.py in staged runtime."""
import asyncio
import json
import sys
import traceback
import subprocess
import os
import shutil
import types
import time
from pathlib import Path


def main():
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    state = Path(config["request"]["stateDir"])
    # Windows rewrites TEMP during AppContainer startup. Restore the authorized state directory.
    for name in ("TEMP", "TMP", "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA"):
        os.environ[name] = str(state)
    # CPython 3.13's default pipe namespace is denied in an AppContainer.
    # Scope the adapter to asyncio.windows_utils, never the shared tempfile module.
    import asyncio.windows_utils as windows_utils
    original_mktemp = windows_utils.tempfile.mktemp
    def local_pipe_name(**kwargs):
        kwargs["prefix"] = kwargs["prefix"].replace("\\\\.\\pipe\\", "\\\\.\\pipe\\LOCAL\\", 1)
        return original_mktemp(**kwargs)
    windows_utils.tempfile = types.SimpleNamespace(mktemp=local_pipe_name)
    sys.stdout = (state / "stdout.txt").open("w", encoding="utf-8")
    sys.stderr = (state / "stderr.txt").open("w", encoding="utf-8")
    if config.get("hang"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        (state / "child-pid").write_text(str(child.pid))
        time.sleep(60)
        return 1
    checks = {}
    for name, filename in config["deniedReads"].items():
        try:
            Path(filename).read_bytes()
            checks[name] = {"denied": False}
        except PermissionError as error:
            checks[name] = {"denied": True, "winerror": error.winerror}
    target = Path(config["allowedRead"])
    checks["snapshotRead"] = bool(target.read_bytes())
    try:
        with target.open("ab") as stream:
            stream.write(b"UNEXPECTED-WRITE")
        checks["snapshotWriteDenied"] = False
    except PermissionError:
        checks["snapshotWriteDenied"] = True
    (state / "write-marker").write_text("state writable")
    checks["stateWrite"] = True
    async def async_tool():
        try:
            child = await asyncio.create_subprocess_exec("rg", "--version", stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.PIPE)
            await child.communicate()
            return {"exit": child.returncode}
        except Exception:
            return {"trace": traceback.format_exc()}
    checks["asyncTool"] = asyncio.run(async_tool())
    checks["tools"] = {}
    # rga resolves its own cache through the Windows known-folder API, which an AppContainer
    # denies. Install the compat shim first so the checks below run the same way the engine does.
    from sirchmunk_compat import install_rga_cache_path
    rga_cache = state / "rga-cache"
    checks["rgaCacheFlag"] = install_rga_cache_path(rga_cache)
    for name in ("rg", "rga"):
        for command in (name, os.environ["DSH_KNOWLEDGE_" + name.upper()]):
            try:
                probe = subprocess.run([command, "--version"], capture_output=True, timeout=5)
                checks["tools"][command] = {"exit": probe.returncode, "found": shutil.which(command),
                                            "stderr": probe.stderr.decode(errors="replace")[:2000]}
            except OSError as error:
                checks["tools"][command] = {"error": type(error).__name__, "winerror": error.winerror}
        if name == "rga":
            # The bare invocation documents the upstream limitation; this one is the engine path.
            for label, argv in (("rga (bare, documented limitation)", [os.environ["DSH_KNOWLEDGE_RGA"], "--version"]),
                                ("rga (with cache path)", [os.environ["DSH_KNOWLEDGE_RGA"],
                                                           "--rga-cache-path=" + str(rga_cache), "--version"])):
                probe = subprocess.run(argv, capture_output=True, timeout=5)
                checks["tools"][label] = {"exit": probe.returncode,
                                          "stderr": probe.stderr.decode(errors="replace")[:2000]}

    async def rga_without_fallback():
        """Real filename search through the same call Sirchmunk uses, with the rg fallback disabled.

        Success here is direct evidence that rga itself served the request.
        """
        from sirchmunk.retrieve.text_retriever import GrepRetriever
        snapshot_dir = str(Path(config["allowedRead"]).parent)
        result = await GrepRetriever._run_rga_async(["--files", snapshot_dir], json_output=False,
                                                    allow_rg_fallback=False)
        return {"returncode": result.get("returncode"), "stdout": (result.get("stdout") or "")[:400],
                "fallback": result.get("fallback_reason")}
    try:
        checks["rgaNoFallback"] = asyncio.run(rga_without_fallback())
    except Exception:
        checks["rgaNoFallback"] = {"trace": traceback.format_exc()[-800:]}
    (state / "boundaries.json").write_text(json.dumps(checks), encoding="utf-8")
    try:
        import worker
        result = asyncio.run(worker.run(config["request"]))
        (state / "result.json").write_text(json.dumps(result), encoding="utf-8")
    except Exception:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
