# Root conftest. No shared fixtures needed yet — each
# test module is self-contained (render_test.py builds its own TmpDirCase helper;
# tests/test_db_collect_contract.py and tests/test_scope_shell.py build their own
# subprocess/psql fixtures). This file exists so `pytest.ini`'s testpaths resolve
# against a single rootdir and so `tests/` is importable as a plain directory
# (no package __init__.py needed — pytest's rootdir-relative import mode handles
# both test roots without collisions since module basenames don't repeat).
