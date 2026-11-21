"""Register the catalog binding and shared isolation for the executor tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SUPPORT = Path(__file__).resolve().parents[1] / "support"
for _name in (
    "suite_isolation", "deployment_isolation", "gate_catalog_binding",
    "gate_estate_fixtures", "hostenv", "gate_invariants_fixtures",
    "gf04_fixtures", "gf03_fixtures", "product_fixtures", "merge_open_prs_fixtures",
    "reservation_process_fixtures", "env_frame_probe",
):
    if _name not in sys.modules:
        _spec = importlib.util.spec_from_file_location(_name, _SUPPORT / f"{_name}.py")
        if _spec is None or _spec.loader is None:
            raise RuntimeError(f"cannot load {_name} from {_SUPPORT}")
        _module = importlib.util.module_from_spec(_spec)
        sys.modules[_name] = _module
        _spec.loader.exec_module(_module)

from deployment_isolation import (  # noqa: E402, F401
    _absent_source_lease_dir,
    _contain_deterministic_git_env,
    _fresh_inventory_cache,
    _private_deploy_marker,
    _worktree_fleet_catalog,
    load_once,
)

load_once("toolgate", Path(__file__).resolve().parents[3] / "scripts/toolgate.py")

from gate_catalog_binding import bind_repository_policy_catalog  # noqa: E402
from suite_isolation import (  # noqa: E402, F401
    _hermetic_subprocess_tripwire,
    _isolated_operation_logs,
    _isolated_ops_catalogue,
    pytest_configure,
)

bind_repository_policy_catalog(Path(__file__).resolve().parents[3])

from merge_open_prs_fixtures import _private_automerge_state  # noqa: E402, F401
