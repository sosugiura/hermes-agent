"""Use the Browser Use CLI 3.0 (https://browser-use.com) for browser automation

When browser.backend is "browser-use", the model gets ``browser_exec`` tool
instead of default browser tools
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from utils import is_truthy_value

logger = logging.getLogger(__name__)

_BACKEND_KEY = "browser-use"
BACKEND_DISABLED = "off"

# Cloud daemon names become the BU_NAME env var
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# Internal marker set by _resolve_backend_cdp on the env dict when the
# resolved browser is EXCLUSIVE to this named session (per-name provider
# browser, or a named Browser Use cloud browser). Popped before the
# subprocess launches — never exported to the CLI.
_PRIVATE_BROWSER_SENTINEL = "_HERMES_BU_PRIVATE_BROWSER"
_OWNED_TARGETS_ENV = "HERMES_BU_OWNED_TARGETS_FILE"
_TARGET_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
_TASK_BROWSER_SESSIONS: Dict[str, Dict[str, Dict[str, Any]]] = {}
_TASK_BROWSER_SESSIONS_LOCK = threading.Lock()
_CLI_DEDICATED_TARGET_SUPPORT: Dict[Tuple[str, ...], bool] = {}
_CLI_DEDICATED_TARGET_SUPPORT_LOCK = threading.Lock()

# Preamble prepended to the model's code for named sessions on SHARED
# browsers (local Chrome / CDP override). The harness daemon attaches to the
# first existing page at startup, so two fresh named daemons can land on the
# SAME tab; steering this daemon onto a tab it created keeps concurrent named
# sessions from clobbering each other before their first new_tab(). Runs
# once per daemon (marker file keyed by BU_NAME under the harness runtime
# state), costs one IPC round-trip on later calls.
_OWN_TAB_PREAMBLE = """\
# hermes: pin this named session to its own tab (once per daemon process)
def _hermes_ensure_own_tab():
    import os as _os, tempfile as _tf
    _name = _os.environ.get("BU_NAME", "default")
    try:
        # Key the marker by the daemon's pid so a daemon restart (which
        # re-attaches to the first shared page) re-pins automatically,
        # while agent-driven tab switches mid-session are left alone.
        from browser_harness import _ipc as _bipc
        _dpid = _bipc.pid_path(_name).read_text().strip() or "0"
    except Exception:
        _dpid = "0"
    _uid = _os.getuid() if hasattr(_os, "getuid") else 0
    _marker = _os.path.join(
        _tf.gettempdir(), "hermes-bu-owntab-%s-%s-%s" % (_uid, _name, _dpid)
    )
    if _os.path.exists(_marker):
        return
    try:
        # Force a fresh target: new_tab() would REUSE a blank current tab,
        # which is exactly the tab a sibling daemon may also hold.
        _tid = cdp("Target.createTarget", url="about:blank").get("targetId")
        if _tid:
            switch_tab(_tid)
    except Exception:
        pass  # best-effort: worst case is pre-fix behavior
    try:
        open(_marker, "w").close()
    except OSError:
        pass
_hermes_ensure_own_tab()
del _hermes_ensure_own_tab
"""

# Opt-in task ownership preamble for shared local/CDP browsers. Dynamic values
# travel through a private env var, never through source interpolation.
_OWNED_TAB_PREAMBLE = """\
# hermes: track tabs owned by this task on a shared browser
import os as _hermes_os, re as _hermes_re
_hermes_owned_path = _hermes_os.environ.get("HERMES_BU_OWNED_TARGETS_FILE", "")
_hermes_generation_path = _hermes_owned_path + ".daemon" if _hermes_owned_path else ""
_hermes_target_re = _hermes_re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
def _hermes_append_line(path, value):
    try:
        _flags = _hermes_os.O_WRONLY | _hermes_os.O_CREAT | _hermes_os.O_APPEND
        if hasattr(_hermes_os, "O_NOFOLLOW"):
            _flags |= _hermes_os.O_NOFOLLOW
        _fd = _hermes_os.open(path, _flags, 0o600)
        try:
            if hasattr(_hermes_os, "fchmod"):
                _hermes_os.fchmod(_fd, 0o600)
            _hermes_os.write(_fd, (value + "\\n").encode("ascii"))
        finally:
            _hermes_os.close(_fd)
        return True
    except OSError:
        return False
def _hermes_record_target(value):
    if isinstance(value, dict):
        value = value.get("targetId")
    if not isinstance(value, str) or not _hermes_target_re.fullmatch(value):
        return False
    return _hermes_append_line(_hermes_owned_path, value)
def _hermes_write_generation(value):
    if not _hermes_generation_path or not isinstance(value, int) or value <= 0:
        return
    try:
        _flags = _hermes_os.O_WRONLY | _hermes_os.O_CREAT | _hermes_os.O_TRUNC
        if hasattr(_hermes_os, "O_NOFOLLOW"):
            _flags |= _hermes_os.O_NOFOLLOW
        _fd = _hermes_os.open(_hermes_generation_path, _flags, 0o600)
        try:
            if hasattr(_hermes_os, "fchmod"):
                _hermes_os.fchmod(_fd, 0o600)
            _hermes_os.write(_fd, (str(value) + "\\n").encode("ascii"))
        finally:
            _hermes_os.close(_fd)
    except OSError:
        pass
def _hermes_record_owned_descendants():
    try:
        with open(_hermes_owned_path, "r", encoding="ascii") as _f:
            _owned = {
                line.strip()
                for line in _f.readlines(1024 * 1024)
                if _hermes_target_re.fullmatch(line.strip())
            }
        _infos = _hermes_original_cdp("Target.getTargets").get("targetInfos", [])
    except Exception:
        return
    _pages = {
        info.get("targetId"): info
        for info in _infos
        if isinstance(info, dict)
        and info.get("type") == "page"
        and _hermes_target_re.fullmatch(str(info.get("targetId") or ""))
    }
    _lineage = set(_owned)
    _changed = True
    while _changed:
        _changed = False
        for _target_id, _info in _pages.items():
            if _target_id not in _lineage and _info.get("openerId") in _lineage:
                _lineage.add(_target_id)
                _changed = True
    for _target_id in _lineage & set(_pages):
        _hermes_record_target(_target_id)
_hermes_original_cdp = cdp
def cdp(method, *args, **kwargs):
    if method == "Target.closeTarget":
        _hermes_record_owned_descendants()
    _result = _hermes_original_cdp(method, *args, **kwargs)
    if method == "Target.createTarget":
        _hermes_record_target(_result)
    return _result
try:
    _hermes_original_close_tab = close_tab
except NameError:
    _hermes_original_close_tab = None
if _hermes_original_close_tab is not None:
    def close_tab(*args, **kwargs):
        _hermes_record_owned_descendants()
        return _hermes_original_close_tab(*args, **kwargs)
_hermes_original_new_tab = new_tab
def new_tab(*args, **kwargs):
    _result = _hermes_original_new_tab(*args, **kwargs)
    _hermes_record_target(_result)
    return _result
def _hermes_daemon_pid():
    try:
        from browser_harness import _ipc as _hermes_ipc
        _pid = _hermes_ipc.identify(
            _hermes_os.environ.get("BU_NAME", ""), timeout=1.0
        )
    except Exception:
        return None
    return _pid if type(_pid) is int and _pid > 0 else None
def _hermes_read_generation():
    try:
        with open(_hermes_generation_path, "r", encoding="ascii") as _f:
            _value = _f.read(32).strip()
        _pid = int(_value)
    except (FileNotFoundError, OSError, ValueError):
        return None
    return _pid if _pid > 0 else None
def _hermes_ensure_owned_root():
    # The current tab proves ownership only on a daemon's first claim or after
    # live IPC proves that this physical named daemon restarted. In the same
    # daemon, task code may intentionally switch to a pre-existing/foreign tab.
    _journal_exists = bool(
        _hermes_owned_path and _hermes_os.path.exists(_hermes_owned_path)
    )
    _previous_pid = _hermes_read_generation() if _journal_exists else None
    _current_pid = _hermes_daemon_pid()
    if _journal_exists and not (
        _previous_pid is not None
        and _current_pid is not None
        and _previous_pid != _current_pid
    ):
        return
    try:
        _current = current_tab()
    except Exception:
        _current = None
    _current_id = _current.get("targetId") if isinstance(_current, dict) else None
    # Browser Harness >= 0.1.9 guarantees a dedicated target for every
    # non-default BU_NAME. browser_exec checks that capability before this
    # preamble runs, so the first/restarted daemon's current target is owned.
    if _hermes_target_re.fullmatch(str(_current_id or "")):
        if _hermes_record_target(_current_id):
            _hermes_write_generation(_current_pid)
        return
    try:
        _result = _hermes_original_cdp("Target.createTarget", url="about:blank")
        _tid = _result.get("targetId") if isinstance(_result, dict) else None
        if _hermes_record_target(_tid):
            _hermes_write_generation(_current_pid)
        if _tid:
            switch_tab(_tid)
    except Exception:
        pass
_hermes_ensure_owned_root()
del _hermes_ensure_owned_root
"""

def _owned_task_source(code: str) -> str:
    """Run trusted ownership setup before compiling model-authored source."""
    return (
        _OWNED_TAB_PREAMBLE
        + "\nexec(compile("
        + repr(code)
        + ", '<browser_exec>', 'exec'))\n"
    )


_OWNED_TAB_CLEANUP_CODE = """\
# hermes: close only this task's recorded pages and popup descendants
import os, re, time
_path = os.environ.get("HERMES_BU_OWNED_TARGETS_FILE", "")
_valid = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
try:
    with open(_path, "r", encoding="ascii") as _f:
        _owned = {line.strip() for line in _f.readlines(1024 * 1024) if _valid.fullmatch(line.strip())}
except FileNotFoundError:
    _owned = set()
def _targets():
    return cdp("Target.getTargets").get("targetInfos", [])
def _closure(infos):
    pages = {i.get("targetId"): i for i in infos if i.get("type") == "page" and _valid.fullmatch(str(i.get("targetId") or ""))}
    lineage = set(_owned)
    changed = True
    while changed:
        changed = False
        for tid, info in pages.items():
            if tid not in lineage and info.get("openerId") in lineage:
                lineage.add(tid)
                changed = True
    return lineage & set(pages)
def _close(ids):
    for tid in ids:
        try:
            close_tab(tid)
        except Exception:
            try:
                cdp("Target.closeTarget", targetId=tid)
            except Exception:
                pass
_infos = _targets()
_wanted = _closure(_infos)
# Closing the harness's current target makes Browser Harness create a recovery
# about:blank page. Park this daemon on an existing non-owned page first; this
# only changes the daemon attachment and never mutates or closes that page.
_parking = next(
    (
        i.get("targetId")
        for i in _infos
        if i.get("type") == "page"
        and i.get("targetId") not in _wanted
        and _valid.fullmatch(str(i.get("targetId") or ""))
    ),
    None,
)
if _parking:
    try:
        switch_tab(_parking)
    except Exception:
        pass
_close(_wanted)
_remaining = set(_wanted)
for _ in range(5):
    if not _remaining:
        break
    time.sleep(0.1)
    _remaining &= {i.get("targetId") for i in _targets() if i.get("type") == "page"}
if _remaining:
    _close(_remaining)
    for _ in range(5):
        time.sleep(0.1)
        _remaining &= {i.get("targetId") for i in _targets() if i.get("type") == "page"}
        if not _remaining:
            break
if _remaining:
    raise RuntimeError("owned browser targets remained after bounded cleanup")
print("cleanup-ok")
"""

_DEFAULT_TIMEOUT_S = 300
_MIN_TIMEOUT_S = 5
_MAX_TIMEOUT_S = 1800
_STDERR_CAP_CHARS = 4000

# Filesystem-safe task ids for per-task workspace dirs.
_TASK_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")

# Screenshot paths printed by capture_screenshot() in the exec output.
# Two alternatives: POSIX absolute (/tmp/shot.png) and Windows drive-letter
# absolute (C:\Users\...\shot.png or C:/Users/.../shot.png). Browser Use on
# Windows prints native paths — the POSIX-only pattern silently dropped them
# and screenshot_path / the multimodal attach never fired (#83884).
_IMAGE_PATH_RE = re.compile(
    r"((?:[A-Za-z]:[\\/]|/)[^\s\"']+?\.(?:png|jpe?g|webp))", re.IGNORECASE
)

# Screenshot discovery runs synchronously in a gateway worker after the
# browser-use subprocess exits.  Keep the scan bounded: third-party CLI output
# can contain megabytes of URLs/DOM paths, and the legacy unanchored regex above
# retried from every slash when no image suffix followed (quadratic worst case,
# holding the GIL for minutes).  One MiB is far beyond a normal printed path but
# still gives the gateway a fixed upper bound.  A screenshot printed earlier
# than this tail is intentionally not attached rather than risking gateway
# starvation.  The regex remains exported for compatibility with callers/tests
# that validate its cross-platform syntax.
_SCREENSHOT_SCAN_TAIL_CHARS = 1024 * 1024
_IMAGE_SUFFIXES = (".jpeg", ".webp", ".png", ".jpg")

# http(s) URL literals in exec code checked against browser_navigate's policy
_URL_RE = re.compile(r"https?://[^\s'\"\\)]+", re.IGNORECASE)


def _blocked_url_in_code(code: str) -> Optional[str]:
    """Return an error if a URL literal fails the built-in navigation checks."""
    from tools.browser_tool import evaluate_url_safety

    for url in _URL_RE.findall(code or ""):
        err = evaluate_url_safety(url)
        if err:
            return err.get("error", "Blocked: unsafe URL")
    return None


def _base_subprocess_env() -> dict:
    from tools.browser_tool import _build_browser_env

    env = _build_browser_env()
    # The browser-use CLI runs under its own Python (uv tool / uvx), which
    # may differ from Hermes's venv Python. PYTHONPATH/PYTHONHOME inherited
    # from the agent process point at Hermes's venv site-packages, and a
    # child interpreter honors them ahead of its own site-packages — so the
    # CLI imports compiled C-extensions (e.g. pydantic_core) built for the
    # wrong interpreter and crashes on ABI mismatch (#83427, #84841, #86006,
    # #86104). Strip both — the CLI manages its own environment and never
    # needs Hermes's import path.
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env.setdefault("ANONYMIZED_TELEMETRY", "false")
    return env


def _read_browser_cfg() -> dict:
    """Return the ``browser:`` config section, or {} on any failure."""
    try:
        from hermes_cli.config import cfg_get, read_raw_config

        cfg = cfg_get(read_raw_config(), "browser", default={})
        return cfg if isinstance(cfg, dict) else {}
    except Exception as e:
        logger.debug("Could not read browser config section: %s", e)
        return {}


def get_browser_backend() -> str:
    """Return the configured browser backend key ("" = unset → default).

    YAML 1.1 parses an unquoted ``off`` as boolean False — a hand-edited
    ``backend: off`` must mean BACKEND_DISABLED, not "unset". (True has no
    sensible backend meaning; normalize it to unset.)
    """
    raw = _read_browser_cfg().get("backend")
    if raw is False:
        return BACKEND_DISABLED
    if raw is True:
        return ""
    return str(raw or "").strip().lower()


def is_legacy_browser_use_cloud_config(browser_cfg: dict) -> bool:
    """True for pre-CLI direct-API Browser Use cloud configs"""
    if not isinstance(browser_cfg, dict):
        return False
    if browser_cfg.get("backend"):
        return False  # an explicit backend choice wins
    provider = str(browser_cfg.get("cloud_provider") or "").strip().lower()
    if provider not in {"browser-use", ""}:
        return False  # explicit local/Browserbase/… choices win
    if is_truthy_value(browser_cfg.get("use_gateway"), default=False):
        return False
    # Camofox is selected via env var, not cloud_provider — a Camofox user
    # with a stray BROWSER_USE_API_KEY must keep their explicit choice.
    try:
        from tools.browser_camofox import is_camofox_mode

        if is_camofox_mode():
            return False
    except Exception as e:
        logger.debug("Camofox activity check failed during migration: %s", e)
    return bool(os.getenv("BROWSER_USE_API_KEY"))


def is_browser_use_cli_mode() -> bool:
    """True when the Browser Use CLI replaces the built-in browser stack.

    Browser Use mode is the DEFAULT: an unset ``browser.backend`` ("") enables
    it whenever the browser-use CLI is runnable (installed binary or uvx).
    Set ``browser.backend: off`` (or ``/browser use off``) for the built-in
    browser_* tools.

    Camofox always falls back to the built-in tools regardless of
    ``browser.backend`` — it is Firefox-based with a custom HTTP API and no
    CDP surface, so the CDP-only browser-use harness cannot drive it.
    """
    try:
        from tools.browser_camofox import is_camofox_mode

        if is_camofox_mode():
            return False
    except Exception as e:
        logger.debug("Camofox activity check failed: %s", e)
    backend = get_browser_backend()
    if backend:
        return backend == _BACKEND_KEY
    if is_legacy_browser_use_cloud_config(_read_browser_cfg()):
        return True
    # Default (backend unset): Browser Use mode when the CLI can run at all;
    # otherwise keep the built-in tools so browsing never silently breaks.
    return _find_cli() is not None


_NOTICE_STAMP_NAME = ".browser_use_default_notice"
_NOTICE_INTERVAL_S = 24 * 3600


def default_downgrade_notice() -> Optional[str]:
    """One-line notice when the default Browser Use backend silently downgraded.

    Returns the notice string when ``browser.backend`` is unset (Browser Use
    would be the default) but the CLI is not runnable, so the session fell
    back to the built-in browser tools. Rate-limited to once per 24h via a
    stamp file so it nudges without nagging. Returns ``None`` otherwise.
    """
    try:
        if get_browser_backend():
            return None  # explicit choice — nothing downgraded
        try:
            from tools.browser_camofox import is_camofox_mode

            if is_camofox_mode():
                return None
        except Exception:
            pass
        if _find_cli() is not None:
            return None

        from hermes_constants import get_hermes_home

        stamp = Path(get_hermes_home()) / "cache" / _NOTICE_STAMP_NAME
        try:
            if 0 <= time.time() - stamp.stat().st_mtime < _NOTICE_INTERVAL_S:
                return None
        except OSError:
            pass
        try:
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.touch()
        except OSError:
            pass
        return (
            "Browser Use CLI not found — using the built-in browser tools. "
            "Run `hermes tools` (Browser Automation → Browser Use) to install it, "
            "or `browser.backend: off` in config.yaml to silence this."
        )
    except Exception as e:  # pragma: no cover — a notice must never break startup
        logger.debug("browser-use downgrade notice failed: %s", e)
        return None


def _managed_bin_dir() -> Optional[str]:
    """Hermes' own bin dir ($HERMES_HOME/bin) — where install.sh puts uv/uvx
    and where install_cli() links the browser-use binary."""
    try:
        from hermes_constants import get_hermes_home

        return str(Path(get_hermes_home()) / "bin")
    except Exception as e:  # pragma: no cover — defensive
        logger.debug("Could not resolve managed bin dir: %s", e)
        return None


def _user_local_bin_dir() -> Optional[str]:
    """The standard user-level tool dir (~/.local/bin on POSIX; uv's default
    tool bin dir on Windows). Desktop/TUI workers may start with a minimal
    PATH that omits it even when `uv tool install browser-use` put the
    binary there."""
    try:
        if os.name == "nt":
            base = os.environ.get("APPDATA")
            if base:
                return str(Path(base) / "uv" / "bin")
            return None
        return str(Path(os.path.expanduser("~")) / ".local" / "bin")
    except Exception as e:  # pragma: no cover — defensive
        logger.debug("Could not resolve user-local bin dir: %s", e)
        return None


def _find_cli() -> Optional[List[str]]:
    """Locate the browser-use CLI, or None when it can't be run.

    MANAGED-FIRST resolution: Hermes' own ``$HERMES_HOME/bin`` copy — the
    one every browser backend selection installs and updates via
    ``install_cli()`` — always wins, so all sessions drive one canonical,
    Hermes-controlled binary. PATH and the user-level tool dir
    (~/.local/bin / %APPDATA%\\uv\\bin, where a manual ``uv tool install``
    links binaries) are fallbacks for setups that never ran our install,
    and cover Desktop/TUI workers that spawn with a minimal PATH. The uvx
    zero-install path (same probe order) is the final fallback.
    """
    probe_paths = (_managed_bin_dir(), None, _user_local_bin_dir())
    for probe_path in probe_paths:
        if probe_path is None or probe_path:
            direct = shutil.which("browser-use", path=probe_path)
            if direct:
                return [direct]
    for probe_path in probe_paths:
        if probe_path is None or probe_path:
            uvx = shutil.which("uvx", path=probe_path)
            if uvx:
                return [uvx, "browser-use"]
    return None


def install_cli(timeout_s: int = 600) -> Tuple[bool, str]:
    """Install the browser-use CLI persistently via ``uv tool install``.

    Resolution order for uv: Hermes' managed uv (bootstrapped on demand via
    ``hermes_cli.managed_uv.ensure_uv``) → uv on PATH. The binary is linked
    into ``$HERMES_HOME/bin`` (``UV_TOOL_BIN_DIR``) so ``_find_cli()``
    resolves it for every profile without touching the user's PATH.

    Returns ``(ok, message)`` — never raises.
    """
    # MANAGED-FIRST: only the managed copy short-circuits the install. A
    # browser-use found on PATH is a user-level side install — it must NOT
    # prevent provisioning the canonical Hermes-managed copy, or resolution
    # stays pinned to a binary we don't control (version drift, no updates
    # through hermes tools).
    bin_dir = _managed_bin_dir()
    if bin_dir:
        managed = shutil.which("browser-use", path=bin_dir)
        if managed:
            return True, f"browser-use CLI already installed ({managed})"

    uv_bin: Optional[str] = None
    try:
        from hermes_cli.managed_uv import ensure_uv

        uv_bin = str(ensure_uv() or "") or None
    except Exception as e:
        logger.debug("Managed uv bootstrap unavailable: %s", e)
    if not uv_bin:
        uv_bin = shutil.which("uv")
    if not uv_bin:
        return False, (
            "uv is not available and could not be bootstrapped. Install uv "
            "(https://docs.astral.sh/uv/) and run `uv tool install browser-use`."
        )

    env = dict(os.environ)
    env["UV_NO_CONFIG"] = "1"
    if bin_dir:
        try:
            Path(bin_dir).mkdir(parents=True, exist_ok=True)
            env["UV_TOOL_BIN_DIR"] = bin_dir
        except OSError as e:
            logger.debug("Could not prepare %s: %s", bin_dir, e)

    try:
        result = subprocess.run(
            [uv_bin, "tool", "install", "browser-use"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return False, f"`uv tool install browser-use` timed out after {timeout_s}s"
    except Exception as e:
        return False, f"Failed to run `uv tool install browser-use`: {e}"

    if result.returncode != 0:
        tail = "\n".join(
            (result.stderr or result.stdout or "").strip().splitlines()[-3:]
        )
        return False, f"`uv tool install browser-use` failed:\n{tail}"

    found = _find_cli()
    if not found or len(found) != 1:
        return False, (
            "install reported success but the browser-use binary is still "
            "not resolvable — run `uv tool install browser-use` manually"
        )
    return True, f"browser-use CLI installed ({found[0]})"


def _workspace_dir(task_id: Optional[str]) -> Optional[str]:
    """Stable per-task scratch dir that persists across browser_exec calls"""
    existing = os.environ.get("BH_AGENT_WORKSPACE")
    if existing:
        return existing
    try:
        from pathlib import Path

        from hermes_constants import get_hermes_home

        safe = _TASK_ID_SAFE_RE.sub("_", str(task_id or "default"))[:80] or "default"
        path = Path(get_hermes_home()) / "cache" / "browser-use" / "workspace" / safe
        path.mkdir(parents=True, exist_ok=True)
        return str(path)
    except Exception as e:
        logger.debug("browser_exec workspace unavailable: %s", e)
        return None


def _find_screenshot(stdout: str, since: float) -> Optional[str]:
    """Return the last screenshot path printed during this exec, or None.

    Only accepts files that exist and were written after the exec started.
    Candidate extraction is a bounded linear scan rather than an unanchored
    regex over arbitrary third-party stdout.
    """
    tail = (stdout or "")[-_SCREENSHOT_SCAN_TAIL_CHARS:]
    # capture_screenshot() prints an unquoted absolute path.  Reverse tokens so
    # the most recent printed screenshot wins, matching the previous behavior.
    for raw_token in reversed(tail.split()):
        token = raw_token.strip("\"'()[]{}<>,;")
        # A path may follow prose punctuation ("saved:/tmp/x.png") or use a
        # Windows drive prefix. Find the first absolute-path marker once.
        path_start = token.find("/")
        for index in range(max(0, len(token) - 2)):
            if (
                token[index].isalpha()
                and (index == 0 or not token[index - 1].isalnum())
                and token[index + 1] == ":"
                and token[index + 2] in "\\/"
            ):
                if path_start < 0 or index < path_start:
                    path_start = index
                break
        if path_start < 0:
            continue

        # Preserve the legacy lazy-regex behavior by stopping at the first
        # supported image suffix after the absolute-path marker.
        lowered = token.lower()
        end = 0
        for suffix in _IMAGE_SUFFIXES:
            suffix_at = lowered.find(suffix, path_start)
            if suffix_at >= 0:
                suffix_end = suffix_at + len(suffix)
                if not end or suffix_end < end:
                    end = suffix_end
        if not end:
            continue
        prefix = token[:end]
        path = prefix[path_start:end]
        try:
            if os.path.isfile(path) and os.path.getmtime(path) >= since - 1:
                return path
        except OSError:
            continue
    return None


def _native_screenshot_result(result: Dict[str, Any], path: str) -> Optional[Dict[str, Any]]:
    """Build a multimodal tool result attaching path for vision models"""
    try:
        from pathlib import Path

        from tools.vision_tools import (
            _resize_image_for_vision,
            _should_use_native_vision_fast_path,
        )

        if not _should_use_native_vision_fast_path():
            return None
        data_url = _resize_image_for_vision(Path(path))
        text = json.dumps(result, ensure_ascii=False)
        return {
            "_multimodal": True,
            "content": [
                {
                    "type": "text",
                    "text": (
                        text
                        + "\n\nThe screenshot from this call is attached — "
                        "inspect it with your native vision."
                    ),
                },
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
            "text_summary": text,
            "meta": {"screenshot_path": path, "native_vision": True},
        }
    except Exception as e:
        logger.debug("Native screenshot attach failed (falling back to text): %s", e)
        return None


def _resolve_backend_cdp(
    env: dict, task_id: Optional[str], session_name: str = ""
) -> Optional[str]:
    """Point the harness at the configured browser backend's CDP endpoint.

    Resolution order (first hit wins):

    1. ``BU_CDP_WS`` / ``BU_CDP_URL`` already in the environment — explicit
       user/operator override, passed through untouched.
    2. ``BROWSER_CDP_URL`` env / ``browser.cdp_url`` config override — the
       ``/browser connect`` path, same precedence the built-in tools honor.
    3. A configured cloud browser provider (Browserbase, Firecrawl, Nous
       gateway/Browser Use cloud, …): reuse the legacy stack's
       ``_get_session_info()`` so browser_exec shares the SAME provider
       session machinery — per-task session cache, expiry replacement,
       inactivity reaper, and atexit cleanup — instead of duplicating it.
    4. Nothing configured: return None; the harness attaches to local
       Chrome (or Browser Use cloud via BU_AUTOSPAWN for legacy configs).

    ``session_name`` (the tool's ``session`` argument / BU_NAME) keys the
    provider session cache when set, so every distinct name gets its OWN
    cloud browser and the same name reuses one — that is what makes named
    sessions actually concurrent-safe on provider backends instead of all
    names sharing a single per-task browser.

    Returns an error string on provider failure, None on success.
    """
    if env.get("BU_CDP_WS") or env.get("BU_CDP_URL"):
        return None

    try:
        from tools.browser_tool import (
            _get_cdp_override,
            _get_cloud_provider,
            _get_session_info,
        )
    except Exception as e:  # pragma: no cover — stubbed browser_tool in tests
        logger.debug("browser_tool backend resolution unavailable: %s", e)
        return None

    try:
        override = _get_cdp_override()
    except Exception:
        override = ""
    if override:
        env["BU_CDP_URL" if override.startswith(("http://", "https://")) else "BU_CDP_WS"] = override
        return None

    try:
        provider = _get_cloud_provider()
    except Exception as e:
        logger.debug("Cloud provider lookup failed: %s", e)
        provider = None
    if provider is None:
        return None

    # Browser Use direct-API configs: the CLI talks to Browser Use cloud
    # natively (BU_AUTOSPAWN / auth login) — routing through the legacy
    # provider here would just create a second, redundant session. The
    # Nous-gateway variant (use_gateway: true) DOES resolve through the
    # provider: the gateway provisions the cloud browser server-side and
    # returns its CDP URL, giving subscribers CLI mode with no raw key.
    provider_key = str(getattr(provider, "name", "") or "").strip().lower()
    if provider_key == _BACKEND_KEY and not is_truthy_value(
        _read_browser_cfg().get("use_gateway"), default=False
    ):
        # Named BU cloud browsers are exclusive to their daemon — no shared
        # tab to isolate from.
        env[_PRIVATE_BROWSER_SENTINEL] = "1"
        return None

    try:
        # Named sessions get their OWN provider browser, keyed by name so the
        # same name reuses one browser across calls and tasks, and different
        # names never collide. Unnamed calls keep the per-task key.
        cache_key = f"bu-named-{session_name}" if session_name else (task_id or "browser-exec-default")
        session_info = _get_session_info(cache_key)
    except Exception as e:
        return (
            f"Cloud browser provider {type(provider).__name__} failed to "
            f"provide a session: {e}. Fix the provider configuration or "
            "switch backends via `hermes tools` → Browser Automation."
        )
    cdp = str((session_info or {}).get("cdp_url") or "")
    if not cdp:
        return (
            f"Cloud browser provider {type(provider).__name__} returned no "
            "CDP endpoint, so Browser Use mode cannot drive it. Switch to "
            "the built-in browser tools for this provider."
        )
    env["BU_CDP_URL" if cdp.startswith(("http://", "https://")) else "BU_CDP_WS"] = cdp
    # A provider browser keyed bu-named-<name> is exclusive to this session —
    # the own-tab preamble is unnecessary there (it would just leak a blank
    # tab into a browser nobody else touches).
    if session_name:
        env[_PRIVATE_BROWSER_SENTINEL] = "1"
    return None


def _task_tab_cleanup_enabled(task_id: Optional[str]) -> bool:
    return bool(task_id) and is_truthy_value(
        _read_browser_cfg().get("close_task_tabs_on_cleanup"), default=False
    )


def _cli_supports_dedicated_named_targets(cmd: List[str]) -> bool:
    """True when Browser Harness guarantees one target per named daemon."""
    key = tuple(cmd)
    with _CLI_DEDICATED_TARGET_SUPPORT_LOCK:
        cached = _CLI_DEDICATED_TARGET_SUPPORT.get(key)
    if cached is not None:
        return cached
    supported = False
    try:
        proc = subprocess.run(
            [*cmd, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            env=_base_subprocess_env(),
        )
        match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", proc.stdout or "")
        supported = bool(
            proc.returncode == 0
            and match
            and tuple(int(part) for part in match.groups()) >= (0, 1, 9)
        )
    except Exception as exc:
        logger.debug("Could not verify Browser Harness version: %s", exc)
    # Cache only positive capability checks. A user can update Browser Use
    # after a failed call and should be able to retry without restarting Hermes.
    if supported:
        with _CLI_DEDICATED_TARGET_SUPPORT_LOCK:
            _CLI_DEDICATED_TARGET_SUPPORT[key] = True
    return supported


def _automatic_task_session(task_id: str, logical_session: str = "") -> str:
    # Keep this short: Browser Harness embeds BU_NAME in an AF_UNIX socket
    # path, whose macOS limit is much lower than _SESSION_RE's 64 characters.
    material = task_id + "\0" + logical_session
    digest = hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()[:20]
    return f"ht-{digest}"


def _owned_targets_path(task_id: str, session_name: str) -> Optional[str]:
    """Return a trusted, non-user-derived state path for owned target IDs."""
    try:
        from hermes_constants import get_hermes_home

        root = Path(get_hermes_home()) / "cache" / "browser-use" / "ownership"
        root.mkdir(parents=True, exist_ok=True)
        try:
            root.chmod(0o700)
        except OSError:
            pass
        key = hashlib.sha256(
            (task_id + "\0" + session_name).encode("utf-8", "replace")
        ).hexdigest()
        return str(root / f"{key}.targets")
    except Exception as exc:
        logger.debug("Could not prepare browser ownership state: %s", exc)
        return None


def _register_task_browser_session(
    task_id: str, session_name: str, env: dict, owned_path: str
) -> None:
    record = {"env": dict(env), "owned_path": owned_path}
    with _TASK_BROWSER_SESSIONS_LOCK:
        _TASK_BROWSER_SESSIONS.setdefault(task_id, {})[session_name] = record


def _task_cleanup_sessions(task_id: str) -> Tuple[str, ...]:
    """Test/debug view of sessions currently registered to a task."""
    with _TASK_BROWSER_SESSIONS_LOCK:
        return tuple(_TASK_BROWSER_SESSIONS.get(str(task_id), {}))


def _owned_page_target_ids(owned: set, target_infos: List[dict]) -> set:
    """Expand recorded page IDs through the CDP opener chain."""
    pages: Dict[str, dict] = {}
    for info in target_infos:
        if not isinstance(info, dict) or info.get("type") != "page":
            continue
        target_id = info.get("targetId")
        if isinstance(target_id, str) and _TARGET_ID_RE.fullmatch(target_id):
            pages[target_id] = info
    lineage = {
        target_id
        for target_id in owned
        if isinstance(target_id, str) and _TARGET_ID_RE.fullmatch(target_id)
    }
    changed = True
    while changed:
        changed = False
        for target_id, info in pages.items():
            if target_id not in lineage and info.get("openerId") in lineage:
                lineage.add(target_id)
                changed = True
    return lineage & set(pages)


def _clear_owned_targets_file(path_value: str) -> bool:
    """Remove an ownership journal, falling back to safe in-place truncation."""
    path = Path(path_value)
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        pass
    try:
        flags = os.O_WRONLY | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        return True
    except OSError:
        logger.debug("Could not clear browser ownership file", exc_info=True)
        return False


def cleanup_browser_use_task(task_id: Optional[str]) -> None:
    """Best-effort close of shared-browser pages owned by one task.

    Failed sessions remain registered (and their 0600 target files remain on
    disk) so a later cleanup attempt can retry. Provider-private browsers are
    intentionally never registered here; their existing provider lifecycle
    remains authoritative.
    """
    # Registration itself is the opt-in proof. Do not re-read the config here:
    # a failed cleanup must remain retryable even if config changes afterward.
    if not task_id:
        return
    task_key = str(task_id)
    with _TASK_BROWSER_SESSIONS_LOCK:
        records = dict(_TASK_BROWSER_SESSIONS.get(task_key, {}))
    if not records:
        return
    cmd = _find_cli()
    if not cmd:
        logger.warning("Browser Use task cleanup skipped: CLI unavailable")
        return
    succeeded = []
    for session_name, record in records.items():
        env = dict(record["env"])
        env["BU_NAME"] = session_name
        env[_OWNED_TARGETS_ENV] = record["owned_path"]
        try:
            proc = subprocess.run(
                cmd,
                input=_OWNED_TAB_CLEANUP_CODE,
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
            )
            if proc.returncode != 0 or "cleanup-ok" not in (proc.stdout or ""):
                logger.warning(
                    "Browser Use task tab cleanup failed for session %s (exit %s)",
                    session_name,
                    proc.returncode,
                )
                continue
            owned_path = record["owned_path"]
            if not _clear_owned_targets_file(owned_path):
                logger.warning(
                    "Browser Use task tabs closed for session %s, but ownership "
                    "state could not be cleared; retaining cleanup registration",
                    session_name,
                )
                # Do not reload: that could create a replacement root while the
                # non-empty journal still describes only the tabs just closed.
                continue
            try:
                Path(owned_path + ".daemon").unlink(missing_ok=True)
            except OSError:
                logger.debug("Could not remove browser daemon generation file", exc_info=True)
            try:
                reload_proc = subprocess.run(
                    [*cmd, "--reload"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=env,
                )
            except Exception:
                logger.warning(
                    "Browser Use task daemon reload failed for session %s; "
                    "owned tabs are already closed",
                    session_name,
                    exc_info=True,
                )
            else:
                if reload_proc.returncode != 0:
                    logger.warning(
                        "Browser Use task daemon reload failed for session %s (exit %s); "
                        "owned tabs are already closed",
                        session_name,
                        reload_proc.returncode,
                    )
            # Tab cleanup plus ownership-state clearing are authoritative.
            # Retrying after a reload-only failure can make the daemon create a
            # replacement blank target that was never recorded, then leave it.
            succeeded.append(session_name)
        except Exception:
            logger.warning(
                "Browser Use task tab cleanup failed for session %s",
                session_name,
                exc_info=True,
            )
    if succeeded:
        with _TASK_BROWSER_SESSIONS_LOCK:
            current = _TASK_BROWSER_SESSIONS.get(task_key, {})
            for session_name in succeeded:
                current.pop(session_name, None)
            if not current:
                _TASK_BROWSER_SESSIONS.pop(task_key, None)


def browser_exec(
    code: str,
    session: str = "",
    timeout_s: int = _DEFAULT_TIMEOUT_S,
    task_id: Optional[str] = None,
):
    """Run Python code through the browser-use CLI, and return its output"""
    from tools.registry import tool_error, tool_result

    if not code or not code.strip():
        return tool_error("No code provided. Pass Python that uses the pre-imported helpers, e.g. new_tab(\"https://example.com\") then print(page_info()).")

    blocked = _blocked_url_in_code(code)
    if blocked:
        return tool_error(blocked)

    cmd = _find_cli()
    if not cmd:
        return tool_error(
            "browser-use CLI not found on PATH, and uvx is unavailable for a "
            "zero-install run. Install it with `uv tool install browser-use` "
            "(or `pipx install browser-use`), then run `browser-use --doctor` "
            "to verify the setup."
        )

    cleanup_enabled = _task_tab_cleanup_enabled(task_id)
    if cleanup_enabled:
        # The caller's session is a logical label only. Physical BU_NAME must
        # include task_id so two concurrent tasks can never share one daemon.
        session = _automatic_task_session(str(task_id), session)

    env = _base_subprocess_env()
    if session:
        if not _SESSION_RE.match(session):
            return tool_error(
                f"Invalid session name {session!r}: use 1-64 letters, digits, "
                "dashes, or underscores (e.g. 'r7k2')."
            )
        env["BU_NAME"] = session
    # Route through the configured browser backend (Browserbase, Firecrawl,
    # Nous gateway, CDP override, local Chrome, …). Named sessions compose
    # with the backend: BU_NAME namespaces the harness daemon (its IPC
    # socket, log, and pid), and on provider backends the name additionally
    # keys its own cloud browser — so concurrent sessions stop clobbering
    # each other's daemon (#86894). Browser Use direct-API cloud configs
    # are the one exception: the CLI manages named cloud browsers natively,
    # and _resolve_backend_cdp skips provider resolution for them.
    backend_err = _resolve_backend_cdp(env, task_id, session_name=session)
    if backend_err:
        return tool_error(backend_err)

    # On a SHARED browser (local Chrome / CDP override), use only Browser
    # Harness versions that guarantee one dedicated target per named daemon.
    # Private per-name browsers skip this ownership layer.
    private_browser = env.pop(_PRIVATE_BROWSER_SENTINEL, None)
    owned_path = None
    if cleanup_enabled and not private_browser:
        if not _cli_supports_dedicated_named_targets(cmd):
            return tool_error(
                "Task-owned browser tab cleanup requires Browser Harness "
                "0.1.9 or newer so every named daemon has a dedicated tab. "
                "Update Browser Use with `browser-use --update -y` and retry."
            )
        owned_path = _owned_targets_path(str(task_id), session)
        if owned_path:
            env[_OWNED_TARGETS_ENV] = owned_path
            code = _owned_task_source(code)
    elif session and not private_browser:
        code = _OWN_TAB_PREAMBLE + code

    workspace = _workspace_dir(task_id)
    if workspace:
        env["BH_AGENT_WORKSPACE"] = workspace

    # BU_AUTOSPAWN makes the CLI start a Browser Use cloud browser when no
    # local Chrome/CDP endpoint is reachable (their API key authenticates it)
    if "BU_AUTOSPAWN" not in env and is_legacy_browser_use_cloud_config(_read_browser_cfg()):
        env["BU_AUTOSPAWN"] = "1"

    if owned_path:
        _register_task_browser_session(str(task_id), session, env, owned_path)

    try:
        timeout = max(_MIN_TIMEOUT_S, min(int(timeout_s), _MAX_TIMEOUT_S))
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT_S

    # Windows: hide the console the .cmd shim would flash (as browser_tool does)
    popen_extra: dict = {}
    if os.name == "nt":
        try:
            from hermes_cli._subprocess_compat import windows_hide_flags

            popen_extra["creationflags"] = windows_hide_flags()
            _si = subprocess.STARTUPINFO()
            _si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            popen_extra["startupinfo"] = _si
        except Exception as e:
            logger.debug("Windows hide-flags unavailable: %s", e)

    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            input=code,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            **popen_extra,
        )
    except subprocess.TimeoutExpired:
        return tool_error(
            f"browser-use exec timed out after {timeout}s. The daemon may "
            "still be working; retry with a larger timeout_s (max "
            f"{_MAX_TIMEOUT_S}), or split the work into several calls that "
            "append to workspace files — anything already written to the "
            "workspace is preserved."
        )
    except OSError as e:
        return tool_error(f"Failed to launch browser-use CLI: {e}")

    result = {
        "success": proc.returncode == 0,
        "exit_code": proc.returncode,
        "output": proc.stdout,
    }
    if workspace:
        result["workspace"] = workspace
    if session:
        result["session"] = session
    stderr = (proc.stderr or "").strip()
    if stderr:
        if len(stderr) > _STDERR_CAP_CHARS:
            stderr = stderr[:_STDERR_CAP_CHARS] + "\n… (stderr truncated)"
        result["stderr"] = stderr

    screenshot = _find_screenshot(proc.stdout, started)
    if screenshot:
        result["screenshot_path"] = screenshot
        native = _native_screenshot_result(result, screenshot)
        if native is not None:
            return native
    return tool_result(result)


# The tool description is the CLI's skill, fetched from browser-use skill
_HEADER_BASE = (
    "Drive a real web browser via the Browser Use CLI. The `code` argument "
    "is piped verbatim to the `browser-use` CLI on stdin and executed as "
    "full Python (standard library available) with the CLI's pre-imported "
    "browser helpers; stdout comes back in the result. Start `code` with a "
    "one-line comment describing the step for the user in plain, "
    "non-technical language, max 60 chars (e.g. `# Searching Amazon for "
    "paper towels`) — the UI displays it as the step label.\n\n"
    "STATE: the browser session and the workspace persist across calls; "
    "Python variables do NOT (each call is a fresh interpreter). The "
    "workspace is a stable directory — path in $BH_AGENT_WORKSPACE and "
    "returned as `workspace` in every result. For multi-item tasks "
    "('collect all N products / every entry / the full table'), append each "
    "batch to a JSON/CSV file in the workspace as you go, then read it back "
    "to assemble the final answer; define reusable functions in "
    "agent_helpers.py there — the harness auto-imports it into every call. "
    "Do aggregation in code, not in your head: dedupe, count, sort, and "
    "format with Python inside the exec. Before giving a final answer on a "
    "multi-item task, verify the collected count against what was asked "
    "and go back for anything missing.\n\n"
    "Batch each sub-procedure (navigate, wait, extract, act) into one call "
    "— do not spend a call per action — but for long extractions prefer "
    "several medium calls that append to workspace files over one giant "
    "call, so progress survives timeouts. For an isolated concurrent "
    "browser session (parallel tasks that must not share tabs), pass "
    "session=<name> (never BU_NAME env syntax) and reuse the same name on "
    "every related call."
)

_HEADER_VISION = (
    " Screenshots are attached to your context automatically: when the exec "
    "output contains a capture_screenshot() path, the image arrives with "
    "this tool's result and you inspect it directly with your own vision — "
    "never send browser screenshots to a separate vision tool."
)

_HEADER_TEXT_ONLY = (
    " Your model cannot view images, so work text-first: page_info() for "
    "state, js() for reading/extracting DOM text, fill_input(selector, "
    "text) for inputs, and js(\"document.querySelector('…').click()\") for "
    "clicks — skip the screenshot-driven workflow described below."
)

_DESCRIPTION_HEADER = _HEADER_BASE  # back-compat alias for external imports

# NOTE: browser_exec is additionally gated at tool-definition time — sessions
# whose resolved toolsets do not include ``terminal`` never see it (see
# model_tools._compute_tool_definitions). The check_fn registered below only
# answers "is Browser Use mode configured"; surface policy lives with the
# session, not in the process-wide TTL-cached check_fn.


def _description_header() -> str:
    """Header tailored to whether the active model can see images natively"""
    try:
        from tools.vision_tools import _should_use_native_vision_fast_path

        if _should_use_native_vision_fast_path():
            return _HEADER_BASE + _HEADER_VISION
    except Exception:
        pass
    return _HEADER_BASE + _HEADER_TEXT_ONLY

_skill_text_cache: Optional[str] = None
_skill_text_fetched = False

# Pinned quick-reference for the CLI's pre-imported helpers. Replaces the
# live ``browser-use skill`` fetch: embedding whatever text the installed CLI
# version prints would ship uncontrolled third-party content into every
# session's system-side schema (version drift across machines, supply-chain
# exposure, and a byte-unstable prompt). A/B benchmarked Aug 2026 (108 runs,
# opus-4.8 + kimi-k3, 6 multi-step tasks x 3 reps): header-only schema went
# 36/36 vs 36/36 for the full skill dump at ~equal tokens (-60% vs the
# legacy browser_* toolset either way). The pinned digest below keeps the
# first-call reliability of the helper names without the 7.7KB dump.
_HELPERS_DIGEST = (
    "\n\nHELPERS (pre-imported): new_tab(url) opens/navigates (use for the "
    "FIRST navigation), goto_url(url) navigates the current tab, "
    "wait_for_load() after navigation, page_info() summarizes the current "
    "page state, js(expr) evaluates a JS expression and returns its value "
    "(js('document.title'); wrap function bodies as js('(() => {...})()') — "
    "a bare '() => {...}' returns the function itself, uncalled), "
    "fill_input(selector, text) types into inputs, click_at_xy(x, y) clicks "
    "viewport coordinates, capture_screenshot() saves and prints a "
    "screenshot path, cdp('Domain.method', **kwargs) is raw CDP — "
    "cdp('Accessibility.getFullAXTree')['nodes'] lists every element's "
    "role/name/backendDOMNodeId (filter in Python before printing; it is "
    "thousands of nodes), then cdp('DOM.getBoxModel', backendNodeId=n) gives "
    "click coordinates. ensure_real_tab() recovers from a stale/internal "
    "tab. Login walls: stop and ask the user; never guess credentials."
)


def _cli_skill_text() -> str:
    """Deprecated: always returns "" — the schema uses the pinned header.

    Kept so tests and any external callers keep importing a stable symbol;
    see _HELPERS_DIGEST for the rationale (benchmark-backed removal of the
    live ``browser-use skill`` fetch).
    """
    return _skill_text_cache or ""


def _dynamic_schema_overrides() -> dict:
    return {"description": _description_header() + _HELPERS_DIGEST}


BROWSER_EXEC_SCHEMA = {
    "name": "browser_exec",
    # Static fallback, used only when the CLI (and uvx) is unavailable
    "description": (
        _HEADER_BASE
        + _HELPERS_DIGEST
        + "\n\n(The browser-use CLI is not installed yet. Install it with "
        "`uv tool install browser-use`.)"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python code to execute using the pre-imported browser helpers. Use print(...) for any data you need back.",
            },
            "session": {
                "type": "string",
                "description": "Named isolated browser session (sets BU_NAME): each name gets its own harness daemon — and on cloud backends its own browser — so concurrent tasks don't clobber each other. Omit for the shared default session. Reuse the same name across calls to keep working in that session (and the name passed to start_remote_daemon(), if used).",
            },
            "timeout_s": {
                "type": "integer",
                "description": f"Max seconds to wait for the code to finish (default {_DEFAULT_TIMEOUT_S}, max {_MAX_TIMEOUT_S}).",
                "default": _DEFAULT_TIMEOUT_S,
            },
        },
        "required": ["code"],
    },
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
from tools.registry import registry

registry.register(
    name="browser_exec",
    toolset="browser-use",
    schema=BROWSER_EXEC_SCHEMA,
    handler=lambda args, **kw: browser_exec(
        code=args.get("code", ""),
        session=args.get("session", "") or "",
        timeout_s=args.get("timeout_s", _DEFAULT_TIMEOUT_S),
        task_id=kw.get("task_id"),
    ),
    check_fn=is_browser_use_cli_mode,
    dynamic_schema_overrides=_dynamic_schema_overrides,
    emoji="🌐",
)
