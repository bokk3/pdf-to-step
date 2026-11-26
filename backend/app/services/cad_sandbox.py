"""Subprocess executor for LLM-generated build123d code (complex_3d fallback only).

Defence in depth, best effort (this is NOT a security boundary against a
determined attacker; it stops accidental or casual misuse by generated code):

1. Static AST gate: only ``build123d`` / ``math`` / ``numpy`` imports, no dunder
   attribute access, no exec/eval/open/compile/getattr & friends.
2. Separate interpreter (``python -I``) with a scrubbed environment, a fresh
   temporary working directory and a restricted ``__builtins__``.
3. Hard wall-clock timeout (default 15 s); the process tree is killed on expiry.

The child writes the resulting solid as a native BREP file which the parent
re-imports; no pickles or meshes cross the boundary.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

RUNNER = Path(__file__).with_name("_sandbox_runner.py")
READY_MARK = "@@B3D_SANDBOX_READY@@"
ALLOWED_MODULES = {"build123d", "math", "numpy"}
FORBIDDEN_NAMES = {
    "open", "exec", "eval", "compile", "__import__", "input", "breakpoint", "globals", "locals",
    "vars", "getattr", "setattr", "delattr", "exit", "quit", "help", "memoryview", "dir",
    "os", "sys", "subprocess", "shutil", "pathlib", "socket", "importlib", "builtins", "type",
    "export_step", "export_stl", "export_brep", "export_gltf", "export_obj", "import_step",
    "import_brep", "import_stl", "import_svg", "ExportSVG", "ExportDXF", "Mesher",
}


class SandboxRejected(ValueError):
    pass


@dataclass
class SandboxResult:
    ok: bool
    brep_path: Path | None
    duration_s: float
    stdout: str
    stderr: str
    error: str | None = None
    info: dict | None = None


def check_code(code: str) -> None:
    """Raise SandboxRejected if the code uses anything outside the allow-list."""
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise SandboxRejected(f"SyntaxError: {exc}") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in ALLOWED_MODULES:
                    raise SandboxRejected(f"import of '{a.name}' is not allowed")
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "").split(".")[0] not in ALLOWED_MODULES:
                raise SandboxRejected(f"import from '{node.module}' is not allowed")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise SandboxRejected(f"access to private attribute '.{node.attr}' is not allowed")
            if node.attr in {"wrapped", "system", "popen"}:
                raise SandboxRejected(f"access to '.{node.attr}' is not allowed")
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES or node.id.startswith("__"):
                raise SandboxRejected(f"use of name '{node.id}' is not allowed")
        elif isinstance(node, (ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.Await)):
            raise SandboxRejected(f"{type(node).__name__} is not allowed")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "__" in node.value:
            raise SandboxRejected("string constants containing '__' are not allowed")
    if not any(
        isinstance(n, (ast.Assign, ast.AnnAssign))
        and any(isinstance(t, ast.Name) and t.id == "result" for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))
        for n in ast.walk(tree)
    ) and not any(isinstance(n, ast.With) for n in ast.walk(tree)):
        raise SandboxRejected("code must assign the final solid to a variable named `result`")


def _scrubbed_env(workdir: Path) -> dict[str, str]:
    keep = ("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update({"TEMP": str(workdir), "TMP": str(workdir), "HOME": str(workdir), "USERPROFILE": str(workdir),
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"})
    return env


def run_build123d_code(code: str, out_path: Path, timeout_s: float = 15.0,
                       import_timeout_s: float = 90.0) -> SandboxResult:
    """Execute generated code in an isolated interpreter; on success ``out_path`` holds a BREP.

    ``timeout_s`` bounds the execution of the generated code itself; interpreter start-up and the
    build123d import are bounded separately by ``import_timeout_s``.
    """
    t0 = time.monotonic()
    try:
        check_code(code)
    except SandboxRejected as exc:
        return SandboxResult(False, None, 0.0, "", "", error=f"rejected: {exc}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="b3d_sandbox_") as tmp:
        tmpdir = Path(tmp)
        code_file = tmpdir / "feature_code.py"
        code_file.write_text(code, encoding="utf-8")
        tmp_out = tmpdir / "result.brep"
        # -I: isolated mode (ignores PYTHON* env vars, user site-packages and the cwd on sys.path)
        cmd = [sys.executable, "-I", str(RUNNER), str(code_file), str(tmp_out)]
        env = _scrubbed_env(tmpdir)
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(cmd, cwd=tmpdir, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", **kwargs)
        out_lines: list[str] = []
        err_lines: list[str] = []
        ready = threading.Event()

        def pump_out() -> None:
            for line in proc.stdout:  # type: ignore[union-attr]
                if line.strip() == READY_MARK:
                    ready.set()
                else:
                    out_lines.append(line)
            ready.set()  # EOF also releases the waiter

        def pump_err() -> None:
            for line in proc.stderr:  # type: ignore[union-attr]
                err_lines.append(line)

        threads = [threading.Thread(target=pump_out, daemon=True), threading.Thread(target=pump_err, daemon=True)]
        for th in threads:
            th.start()
        # Interpreter start + `import build123d` (≈5-10 s on Windows) is not charged to the code budget.
        if not ready.wait(import_timeout_s):
            _kill(proc)
            return SandboxResult(False, None, time.monotonic() - t0, "".join(out_lines), "".join(err_lines),
                                 error=f"sandbox interpreter did not start within {import_timeout_s:.0f}s")
        t_exec = time.monotonic()
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill(proc)
            for th in threads:
                th.join(2)
            return SandboxResult(False, None, time.monotonic() - t0, "".join(out_lines)[-4000:],
                                 "".join(err_lines)[-4000:], error=f"timeout: code ran longer than {timeout_s:.0f}s")
        for th in threads:
            th.join(5)
        stdout, stderr = "".join(out_lines), "".join(err_lines)
        dt = time.monotonic() - t_exec
        info = None
        last = stdout.strip().splitlines()[-1] if stdout.strip() else ""
        try:
            info = json.loads(last) if last.startswith("{") else None
        except json.JSONDecodeError:
            info = None
        if proc.returncode != 0 or not tmp_out.exists() or not (info or {}).get("ok"):
            err = (info or {}).get("error") or stderr.strip()[-2000:] or f"exit code {proc.returncode}"
            return SandboxResult(False, None, dt, stdout[-4000:], stderr[-4000:], error=err, info=info)
        out_path.write_bytes(tmp_out.read_bytes())
        return SandboxResult(True, out_path, dt, stdout[-4000:], stderr[-4000:], info=info)


def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
        proc.wait(5)
    except Exception:  # noqa: BLE001
        pass


def _s(v) -> str:
    if v is None:
        return ""
    return v.decode(errors="replace") if isinstance(v, bytes) else str(v)


def load_brep(path: Path):
    from build123d import import_brep

    return import_brep(str(path))
