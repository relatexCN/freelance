"""Compatibility alias for the agent-owned executor.

Both import paths resolve to the same module, including its registry and globals.
"""

import sys

from app.manager_agent.tools import executor

sys.modules[__name__] = executor
