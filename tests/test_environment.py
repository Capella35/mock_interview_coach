"""Environment guard: the installed package must be importable the way the CLI imports it.

On macOS, if `.venv` is (re)created with the "hidden" file flag, Python skips the
hidden `.pth` file of uv's editable install and `import interview_agent` fails.
This test turns that into a clear message with the fix.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

FIX = "run:  chflags -R nohidden .venv   (see CLAUDE.md §4, 'Location')"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS file flags only")
def test_editable_install_pth_is_not_hidden():
    site = Path(sys.prefix) / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    pths = list(site.glob("_editable_impl_interview_agent*.pth"))
    if not pths:
        pytest.skip("not an editable install")
    hidden = [p.name for p in pths if os.stat(p).st_flags & stat.UF_HIDDEN]
    assert not hidden, f"{hidden} carries macOS 'hidden' flag, so Python ignores it — {FIX}"


def test_package_importable_without_test_path_tricks():
    import interview_agent.cli  # noqa: F401  (the CLI's own import path)
