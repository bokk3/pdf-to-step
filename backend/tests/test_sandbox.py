from __future__ import annotations

from pathlib import Path

import pytest

from app.services.cad_sandbox import SandboxRejected, check_code, load_brep, run_build123d_code

GOOD = """
from build123d import *
import math
result = Pos(5, 5, 0) * Cylinder(radius=2, height=10, align=(Align.CENTER, Align.CENTER, Align.MIN))
"""


@pytest.mark.parametrize(
    "code,msg",
    [
        ("import os\nresult = 1", "import of 'os'"),
        ("from subprocess import run\nresult = 1", "import from 'subprocess'"),
        ("result = ().__class__.__bases__", "private attribute"),
        ("result = open('x')", "open"),
        ("result = eval('1')", "eval"),
        ("result = getattr(Box, 'x')", "getattr"),
        ("export_step(Box(1,1,1), 'x.step')\nresult = 1", "export_step"),
        ("x = Box(1, 1, 1)", "result"),
        ("result = '__import__'", "__"),
    ],
)
def test_static_gate_rejects(code, msg):
    with pytest.raises(SandboxRejected, match=msg):
        check_code(code)


def test_rejected_code_never_runs(tmp_path: Path):
    res = run_build123d_code("import os\nresult = 1", tmp_path / "x.brep")
    assert not res.ok and res.error.startswith("rejected") and res.duration_s == 0.0


def test_good_code_produces_brep(tmp_path: Path):
    res = run_build123d_code(GOOD, tmp_path / "good.brep", timeout_s=15)
    assert res.ok, (res.error, res.stderr)
    shape = load_brep(res.brep_path)
    assert shape.volume == pytest.approx(3.14159265 * 4 * 10, rel=1e-6)
    assert res.info["bbox_min"] == pytest.approx([3, 3, 0])


def test_runtime_error_and_timeout(tmp_path: Path):
    bad = run_build123d_code("from build123d import *\nresult = Box(1, 1, 1)\nraise ValueError('boom')",
                             tmp_path / "bad.brep")
    assert not bad.ok and "ValueError: boom" in bad.error

    loop = run_build123d_code("from build123d import *\nwhile True:\n    pass\nresult = Box(1,1,1)",
                              tmp_path / "loop.brep", timeout_s=2)
    assert not loop.ok and "timeout" in loop.error
