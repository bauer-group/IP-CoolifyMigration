"""Test suite.

This file is not incidental. With it present, pytest inserts the ROOTDIR into
sys.path and `from tests.conftest import FakeHost` resolves. Without it, pytest
inserts `tests/` instead, and that import only works under `python -m pytest`
(which happens to add the CWD) — so it passes locally and dies in CI with
ModuleNotFoundError. Mirrors BG-MCPCore.
"""
