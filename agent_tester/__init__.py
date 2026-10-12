"""QA test agent."""
import warnings as _w

# ADK prints an [EXPERIMENTAL] UserWarning for every feature flag it enables; they are noise in run output.
_w.filterwarnings("ignore", message=r"\[EXPERIMENTAL\]", category=UserWarning)
