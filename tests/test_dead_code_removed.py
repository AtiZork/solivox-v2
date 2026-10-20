"""Regression guard (action-plan 1.1): autosnipe_logic.py and
autosnipe_buy_script.py were deleted - the legacy buy bot they implemented
picked an arbitrary user's wallet/config and crashed its own DB write after
a real on-chain buy. This locks in that nothing reachable from main.py
imports them again by mistake (e.g. a merge bringing an old branch back)."""
import ast
import pathlib

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
REMOVED_MODULES = {"autosnipe_logic", "autosnipe_buy_script"}


def test_deleted_modules_no_longer_exist_on_disk():
    for name in REMOVED_MODULES:
        assert not (PROJECT_ROOT / f"{name}.py").exists(), f"{name}.py should have been deleted"


def test_no_reachable_python_file_imports_the_deleted_modules():
    """Scans every .py file outside tests/ for an import of either removed
    module. Uses the AST rather than a plain grep so a comment or docstring
    mentioning the old name (explaining why it was removed) doesn't fail."""
    offenders = []
    for path in PROJECT_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {node.module} if node.module else set()
            else:
                continue
            if names & REMOVED_MODULES:
                offenders.append(path.name)
    assert not offenders, f"these files still import a deleted module: {offenders}"
