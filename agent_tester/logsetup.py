"""Keep exceptions and library noise out of the console: everything logged goes to logs/agent_tester.log
(and, during a run, to <run>/errors.log for warnings and errors). The console only shows the agent's own
progress lines and one plain sentence per failure. AGENT_DEBUG=1 keeps the old console output."""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

LOG_DIR = Path(os.environ.get("AGENT_LOG_DIR", "logs"))
FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_file_handler: logging.Handler | None = None


def _is_console(h: logging.Handler) -> bool:
    return (isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
            and getattr(h, "stream", None) in (sys.stderr, sys.stdout, sys.__stderr__, sys.__stdout__))


def quiet_libraries() -> None:
    """Libraries (LiteLLM, ADK, MCP...) add their own console handlers when imported; move them to the file."""
    try:
        import litellm
        litellm.suppress_debug_info = True            # the red "Give Feedback / Get Help" banner
        litellm.set_verbose = False
    except Exception:  # noqa: BLE001
        pass
    loggers = [logging.getLogger()] + [l for l in logging.Logger.manager.loggerDict.values()
                                       if isinstance(l, logging.Logger)]
    for lg in loggers:
        for h in list(lg.handlers):
            if _is_console(h):
                lg.removeHandler(h)
        if lg is not logging.getLogger():
            lg.propagate = True


def setup(debug: bool | None = None) -> Path | None:
    """Call once at start-up. Returns the log file path (None in debug mode)."""
    global _file_handler
    if debug is None:
        debug = bool(os.environ.get("AGENT_DEBUG"))
    if debug:
        return None
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / "agent_tester.log"
    root = logging.getLogger()
    if _file_handler is None:
        _file_handler = logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3,
                                                             encoding="utf-8")
        _file_handler.setFormatter(logging.Formatter(FMT))
        root.addHandler(_file_handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    logging.captureWarnings(True)
    quiet_libraries()

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logging.getLogger("agent_tester").error("unhandled exception", exc_info=(exc_type, exc, tb))
        print(f"Error: {exc_type.__name__}: {str(exc)[:200]} (details in {path})", file=sys.stderr)

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        logging.getLogger("agent_tester").error(f"unhandled exception in thread {getattr(args.thread, 'name', '?')}",
                                                exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = hook
    threading.excepthook = thread_hook
    return path


class RunErrors:
    """While a run is going, also copy warnings and errors (with tracebacks) to <run>/errors.log."""

    def __init__(self, run_dir: Path):
        self.path = Path(run_dir) / "errors.log"
        self.h: logging.Handler | None = None

    def __enter__(self):
        if os.environ.get("AGENT_DEBUG"):
            return self
        quiet_libraries()                             # handlers added by imports since start-up
        try:
            self.h = logging.FileHandler(self.path, encoding="utf-8")
        except OSError:
            return self
        self.h.setLevel(logging.WARNING)
        self.h.setFormatter(logging.Formatter(FMT))
        logging.getLogger().addHandler(self.h)
        return self

    def __exit__(self, *exc):
        if self.h is not None:
            logging.getLogger().removeHandler(self.h)
            self.h.close()
        return False


def short_error(e: BaseException, where: str = "") -> str:
    """Log the full exception to the log file and return one short line for the console/report."""
    logging.getLogger("agent_tester").error(f"{where or 'error'}: {type(e).__name__}", exc_info=e)
    text = f"{type(e).__name__}: {e}"
    if any(k in text for k in ("Connection error", "ConnectError", "Name or service not known",
                               "APIConnectionError", "ServiceUnavailable", "503")):
        return "the model server could not be reached"
    first = str(e).strip().splitlines()[0] if str(e).strip() else ""
    return f"{type(e).__name__}: {first[:160]}" if first else type(e).__name__
