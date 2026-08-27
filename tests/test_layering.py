"""Import-graph discipline: the pure classifier must not pull IO modules.

B2's final review (M5): admission.py ("pure functions, no IO by
construction") imported inventory.py (subprocess, os) for its dataclasses,
making yaml+plan_fields an import-time dependency of run_controller and
defeating roadmap.py's guarded lazy import. The verdict dataclasses move
too: without that, the types module would import dag_subset and pull yaml
right back in.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _imports_of(module_rel: str) -> set[str]:
    tree = ast.parse((ROOT / module_rel).read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def test_admission_does_not_import_io_modules():
    imports = _imports_of("dispatcher/core/admission.py")
    assert "dispatcher.core.inventory" not in imports
    assert "dispatcher.core.dag_subset" not in imports


def test_types_module_imports_nothing_heavy():
    imports = _imports_of("dispatcher/core/inventory_types.py")
    forbidden = {
        "subprocess",
        "os",
        "yaml",
        "dispatcher.core.inventory",
        "dispatcher.core.dag_subset",
    }
    assert not (imports & forbidden)
