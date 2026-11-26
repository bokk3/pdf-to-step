"""Child-process entry point for cad_sandbox. Runs with `python -I`.

Usage: python -I _sandbox_runner.py <code_file> <out_brep>
Prints exactly one JSON line on stdout as the last line.
"""

import json
import sys


def _emit(obj):
    sys.stdout.write("\n" + json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    code_file, out_file = sys.argv[1], sys.argv[2]
    import builtins
    import math

    import build123d
    from build123d import BuildPart, export_brep
    from build123d.topology import Shape

    # tell the parent that imports are done; the execution timeout starts now
    sys.stdout.write("@@B3D_SANDBOX_READY@@\n")
    sys.stdout.flush()

    allowed_modules = {"build123d", "math", "numpy"}
    real_import = builtins.__import__

    def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level or name.split(".")[0] not in allowed_modules:
            raise ImportError(f"import of {name!r} is not allowed in the sandbox")
        return real_import(name, globals, locals, fromlist, level)

    safe_names = [
        "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter", "float", "int", "isinstance",
        "len", "list", "map", "max", "min", "pow", "print", "range", "reversed", "round", "set", "sorted",
        "str", "sum", "tuple", "zip", "Exception", "ValueError", "TypeError", "ZeroDivisionError",
        "ArithmeticError", "IndexError", "KeyError", "RuntimeError", "True", "False", "None",
    ]
    safe_builtins = {n: getattr(builtins, n) for n in safe_names if hasattr(builtins, n)}
    safe_builtins["__import__"] = safe_import
    safe_builtins["__build_class__"] = builtins.__build_class__

    ns = {"__builtins__": safe_builtins, "__name__": "sandbox", "math": math}
    ns.update({k: getattr(build123d, k) for k in dir(build123d) if not k.startswith("_")})
    for banned in ("export_step", "export_stl", "export_brep", "export_gltf", "export_obj", "import_step",
                   "import_brep", "import_stl", "import_svg", "ExportSVG", "ExportDXF", "Mesher"):
        ns.pop(banned, None)

    with open(code_file, encoding="utf-8") as fh:
        source = fh.read()
    try:
        exec(compile(source, "feature_code.py", "exec"), ns)  # noqa: S102 - this IS the sandbox
    except Exception as exc:  # noqa: BLE001
        import traceback

        tb = traceback.format_exc(limit=6)
        _emit({"ok": False, "error": f"{type(exc).__name__}: {exc}", "traceback": tb})
        return 2

    result = ns.get("result")
    if result is None:
        for v in ns.values():
            if isinstance(v, BuildPart):
                result = v
    if isinstance(result, BuildPart):
        result = result.part
    if not isinstance(result, Shape):
        _emit({"ok": False, "error": f"`result` must be a build123d Shape, got {type(result).__name__}"})
        return 3
    vol = float(result.volume)
    if vol <= 0:
        _emit({"ok": False, "error": "result has zero volume (not a solid?)"})
        return 4
    bb = result.bounding_box()
    export_brep(result, out_file)
    _emit({"ok": True, "volume": vol, "solids": len(result.solids()),
           "bbox_min": [bb.min.X, bb.min.Y, bb.min.Z], "bbox_max": [bb.max.X, bb.max.Y, bb.max.Z]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
