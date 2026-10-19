"""xrdclient's per-function maintainability gate: xrd-devtools' plugin on ``maintainability.json``.

No function in the policy's paths (``src``, ``tools``, ``tests``,
``benchmarks``, ``examples``) may exceed any of the five absolute limits, and
the policy may be no looser than the family's standard. The tool and its own
tests moved to xrd-devtools; ``docs/maintainability.md`` explains the
metrics. Without an xrd-devtools checkout the gate skips with that
environmental reason (CONTRIBUTING.md).
"""

import pytest

plugin = pytest.importorskip(
    "xrddevtools.pytest_plugin",
    reason="environmental: no xrd-devtools checkout (../xrd-devtools, .dependencies/xrd-devtools)",
)

test_xrdclient_has_no_function_over_a_maintainability_limit = plugin.maintainability_gate(__file__)
