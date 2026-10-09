"""Runs inside the AppContainer: try each configured pipe and report what happened.

Copied beside engine/ipc.py in the staged runtime; writes only into the caller-granted state dir.
"""
import json
from pathlib import Path
import sys


def main():
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    state = Path(config["stateDir"])
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import ipc

    report = {"results": {}}
    for label, path in config["pipes"].items():
        try:
            reply = ipc.roundtrip(path, {"hello": label}, timeout_ms=config.get("connectTimeoutMs", 4000))
            report["results"][label] = {"connected": True, "reply": reply}
        except OSError as error:
            report["results"][label] = {"connected": False, "winerror": getattr(error, "winerror", None),
                                        "message": str(error)[:200]}
        except Exception as error:  # a framing problem must be reported, not crash the probe
            report["results"][label] = {"connected": False, "error": type(error).__name__,
                                        "message": str(error)[:200]}
    # The container must not be able to take over the parent's pipe name.
    if config.get("createAttempt"):
        try:
            server = ipc.PipeServer(config["createAttempt"]).listen()
            server.close()
            report["createAttempt"] = {"denied": False}
        except OSError as error:
            report["createAttempt"] = {"denied": True, "winerror": getattr(error, "winerror", None)}
    (state / "ipc.json").write_text(json.dumps(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
