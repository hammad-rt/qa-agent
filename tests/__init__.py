
import os as _os
# Existing tests cover the interactive prompts; automatic mode has its own tests (test_auto_mode.py).
_os.environ.setdefault("QA_INTERACTIVE", "1")
os.environ.setdefault("LOGIN_LEARN", "0")
