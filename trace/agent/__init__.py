"""Trace — Stage 4: Agent Compiler + Test Deployer (Claude Managed Agents).

The Compiler (``compiler.py``) wraps an approved bundle's ``spec_<id>.md`` into a
Managed Agents agent definition (``agent_<id>.{test,live}.yaml``) — pure
transformation, no network. The Test Deployer (``deployer.py``) runs the compiled
test-mode agent through the API in a sandbox and grades it (``grader.py``) against
the spec's own acceptance criteria. All platform calls go through ``api_client.py``,
pinned to the beta version; everything is offline-testable via an injected client.

Live deployment (OAuth, real MCP connections, scheduling, refinement) is out of
scope here — live envelopes are emitted and schema-validated but never deployed.
"""

from .compiler import compile_agent  # noqa: F401
from .deployer import run_test  # noqa: F401
