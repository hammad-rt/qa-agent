"""Re-export hermetic defaults (works with bare `discover -s tests` when tests/ is on path)."""
from agent_tester.testing_hermetic import *  # noqa: F403
from agent_tester import testing_hermetic as _impl

clear_pack_secrets = _impl.clear_pack_secrets
