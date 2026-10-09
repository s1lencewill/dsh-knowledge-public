"""Process-local dependency and proxy compatibility, without changing parent settings."""
import ipaddress
import os
from pathlib import Path
import shutil


def normalize_no_proxy(value):
    entries = []
    for entry in value.split(","):
        entry = entry.strip()
        if entry.startswith("[") and entry.endswith("]"):
            try:
                ipaddress.IPv6Address(entry[1:-1])
            except ValueError:
                pass
            else:
                entry = entry[1:-1]
        entries.append(entry)
    return ",".join(entries)


def normalize_proxy_environment(environment):
    """Return a copy with every NO_PROXY spelling normalised.

    httpx (0.28.1) and httpx2 both build a URLPattern from each bypass entry, and a bracketed IPv6
    literal such as "[::1]" raises InvalidURL inside the client constructor, before any request is
    sent. Every HTTP client in this project must therefore see the same normalised list.
    """
    env = dict(environment)
    for key in list(env):
        if key.upper() == "NO_PROXY":
            env[key] = normalize_no_proxy(env[key])
    return env


def executable(name, env, project, home):
    """Resolve a tool binary. The project's own pinned copy wins over PATH.

    Order matters for reproducibility: the staged runtime is identified by tool hashes, and a tool
    reached through PATH can be replaced outside the project (a user-scoped `rg.exe` was updated on
    this host and silently invalidated the runtime). A project-local `.local/tools/<name>.exe` - put
    there by scripts/pin-tools.py - is therefore preferred over PATH, while an explicit
    `DSH_KNOWLEDGE_<NAME>` override still wins over everything.
    """
    override = env.get("DSH_KNOWLEDGE_" + name.upper())
    if override:
        path = Path(override)
        if not path.is_absolute() or not path.is_file():
            raise RuntimeError(f"DEPENDENCY_MISSING: DSH_KNOWLEDGE_{name.upper()} must be an absolute executable path")
        return path
    suffix = ".exe" if os.name == "nt" else ""
    pinned = [project / ".local/tools" / (name + suffix)]
    if name == "rga":
        # The controlled build (tools/rga/cache-path-fallback.patch) lives next to the official one.
        pinned.insert(0, project / ".local/tools/rga-patched" / ("rga" + suffix))
    for path in pinned:
        if path.is_file():
            return path
    found = shutil.which(name, path=env.get("PATH", ""))
    if found:
        return Path(found)
    candidates = [project / ".local/tools" / (name + suffix)]
    if name == "rga":
        candidates += sorted((project / ".local/tools").glob("ripgrep_all-*/" + name + suffix))
    if name == "rg":
        candidates += [home / ".hanako/.pi/agent/bin" / (name + suffix)]
        if os.name == "nt":
            candidates += sorted((home / "AppData/Local/OpenAI/Codex/bin").glob("*/rg.exe"))
    for path in candidates:
        if path.is_file():
            return path
    raise RuntimeError(f"DEPENDENCY_MISSING: install {name} and set DSH_KNOWLEDGE_{name.upper()} to its absolute path")


def prepare_environment(environment, project=None, home=None):
    env = normalize_proxy_environment(environment)
    project = project or Path(__file__).resolve().parents[1]
    home = home or Path.home()
    directories = [str(executable(name, env, project, home).resolve().parent) for name in ("rg", "rga")]
    env["PATH"] = os.pathsep.join(dict.fromkeys(directories + [env.get("PATH", "")]))
    return env


def child_environment(environment):
    """Environment for the worker subprocess: UTF-8 mode plus one consistent proxy list.

    Sirchmunk 0.1.0 opens source files with the platform locale encoding, so non-ASCII documents
    arrive as mojibake on a cp936/cp1252 host and no citation can ever be re-located. Python's
    UTF-8 mode is decided at interpreter start-up, so it must be set by whoever spawns the worker,
    not patched inside the running process.
    """
    env = normalize_proxy_environment(environment)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # The engine must import from the interpreter it was launched with, never from a user-level
    # site-packages that happens to shadow it (see appcontainer.container_environment).
    env["PYTHONNOUSERSITE"] = "1"
    return env


def model_settings(mode, env, via_proxy=False):
    if via_proxy:
        # A proxied engine holds no credentials at all: the parent keeps them and calls the provider.
        return {"base_url": "http://127.0.0.1:9/v1", "api_key": "no-direct-credentials",
                "model": "parent-managed"}
    if mode == "FILENAME_ONLY":
        # Never use a live provider for the no-model probe, even if inherited.
        return {"base_url": "http://127.0.0.1:9/v1", "api_key": "filename-only-no-model", "model": "not-configured"}
    names = {"base_url": "LLM_BASE_URL", "api_key": "LLM_API_KEY", "model": "LLM_MODEL_NAME"}
    settings = {key: env.get(name, "").strip() for key, name in names.items()}
    if not all(settings.values()):
        raise RuntimeError("MODEL_NOT_CONFIGURED")
    return settings


class ModelProxyRejected(RuntimeError):
    """Only fixed protocol codes may survive the untrusted engine's error boundary."""
    CODES = frozenset({"REQUEST_TOO_LARGE", "BAD_REQUEST", "FORBIDDEN_PARAMETER",
                       "CALL_BUDGET_EXHAUSTED", "TOKEN_BUDGET_EXHAUSTED", "DEADLINE_EXCEEDED",
                       "JOB_CANCELLED", "JOB_FINISHED", "PROVIDER_ERROR", "INTERNAL_ERROR"})

    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in self.CODES else "MODEL_PROXY_ERROR"
        super().__init__(self.code)


def safe_error(error):
    from evidence import SnapshotCapacityError
    if isinstance(error, SnapshotCapacityError):
        return str(error)
    if isinstance(error, ModelProxyRejected):
        return error.code
    message = str(error)
    if message == "MODEL_NOT_CONFIGURED" or message.startswith("DEPENDENCY_MISSING:"):
        return message
    if type(error).__name__ == "InvalidURL":
        return "MODEL_OR_PROXY_URL_INVALID: check model URL and proxy URL syntax; configuration values are not logged"
    return type(error).__name__
