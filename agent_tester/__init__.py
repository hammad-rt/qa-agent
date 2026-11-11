"""QA test agent."""
import logging
import warnings as _w

# ADK prints an [EXPERIMENTAL] UserWarning for every feature flag it enables; they are noise in run output.
_w.filterwarnings("ignore", message=r"\[EXPERIMENTAL\]", category=UserWarning)

# Library loggers propagate to the root handlers installed by logsetup. A NullHandler
# on the package logger stops lastResort from dumping warnings into unit-test stderr
# when no handlers are configured yet.
logging.getLogger(__name__).addHandler(logging.NullHandler())
