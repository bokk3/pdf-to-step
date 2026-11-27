"""Face and tilted-plane coordinate frames (SPEC_V2_0_MULTISIDE_FIXTURING.md §2), pure numpy (no CAD kernel).

One source of truth for every module that maps feature coordinates (u, v) on a face to the global datum frame:
the compiler, verifier, XCAF colouring, drawing engine, setup planner, shopfloor view and the frontend (mirrored in
``frontend/src/lib/frames.ts``).

Two conventions exist and are chosen by ``PartSpec.schema_version``:

* **v2.0** (default for new specs, right-handed, the spec's table): every face frame satisfies u x v = n (outward),
  the local origin sits on a stock corner so that local coordinates are positive on the face, and cuts go along -n.

  ======  =====================  ========  ========  ========
  face    origin                 normal    +u        +v
  ======  =====================  ========  ========  ========
  top     (0, 0, z1)             +Z        +X        +Y
  bottom  (x0, y1, z0)           -Z        +X        -Y
  front   (x0, y0, z0)           -Y        +X        +Z
  back    (x1, y1, z0)           +Y        -X        +Z
  left    (x0, y1, z0)           -X        -Y        +Z
  right   (x1, y0, z0)           +X        +Y        +Z
  ======  =====================  ========  ========  ========

  (x0..x1, y0..y1, z0..z1 = stock box in datum coordinates; with the default datum ``bottom_left_top_face`` that is
  [0, L] x [0, W] x [-T, 0] and the table equals the spec.) The top face keeps the datum X/Y, so v1 top features
  and v2 top features are identical.

* **v1.x** (stored v1.0 / v1.1 jobs, unchanged): face coordinates are the datum coordinates projected on the face,
  never mirrored (top/bottom (X, Y), front/back (X, Z), left/right (Y, Z)). Left / front / bottom frames are
  left-handed there; the compiler handles that through ``handedness``.

Tilted planes (``face = "plane:<id>"``, v2.0 only): origin ``(origin_x, origin_y, origin_z)`` and the rotation
``SPATIAL_XYZ`` = intrinsic rotations about X, then the new Y, then the new Z (R = Rx(a) . Ry(b) . Rz(c)), like
``PLANE SPATIAL`` / ``G68.2``. Local +Z is the outward normal; cuts go along local -Z.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from app.schemas.part_spec import PartSpec

ORTHO_FACES = ("top", "bottom", "front", "back", "left", "right")
PLANE_PREFIX = "plane:"


@dataclass(frozen=True)
class Frame:
    name: str
    origin: np.ndarray
    u: np.ndarray
    v: np.ndarray
    n: np.ndarray  # outward normal
    u_range: tuple[float, float]  # face extent in local coordinates (tilted planes: generous)
    v_range: tuple[float, float]
    thickness: float  # material depth behind the face (through cuts); stock only
    tilted: bool = False

    def point(self, u: float, v: float, depth: float = 0.0) -> np.ndarray:
        """Global point of local (u, v), ``depth`` into the material (along -n)."""
        return self.origin + u * self.u + v * self.v - depth * self.n

    def local(self, p) -> tuple[float, float, float]:
        """Global point -> (u, v, height above the face along n)."""
        d = np.asarray(p, dtype=float) - self.origin
        return float(d @ self.u), float(d @ self.v), float(d @ self.n)

    @property
    def handedness(self) -> float:
        """+1 when u x v = n (all v2.0 frames), -1 for the mirrored v1 frames."""
        return 1.0 if float(np.cross(self.u, self.v) @ self.n) > 0 else -1.0


def is_legacy(spec: "PartSpec") -> bool:
    return spec.schema_version in ("1.0", "1.1")


def rotation_spatial_xyz(a_deg: float, b_deg: float, c_deg: float) -> np.ndarray:
    """Intrinsic X -> Y -> Z rotation matrix; columns are the local X, Y, Z axes in global coordinates."""
    a, b, c = (math.radians(x) for x in (a_deg, b_deg, c_deg))
    rx = np.array([[1, 0, 0], [0, math.cos(a), -math.sin(a)], [0, math.sin(a), math.cos(a)]])
    ry = np.array([[math.cos(b), 0, math.sin(b)], [0, 1, 0], [-math.sin(b), 0, math.cos(b)]])
    rz = np.array([[math.cos(c), -math.sin(c), 0], [math.sin(c), math.cos(c), 0], [0, 0, 1]])
    return rx @ ry @ rz


def _v(*xs: float) -> np.ndarray:
    return np.array(xs, dtype=float)


X, Y, Z = _v(1, 0, 0), _v(0, 1, 0), _v(0, 0, 1)


def ortho_frames(spec: "PartSpec", legacy: bool | None = None) -> dict[str, Frame]:
    (x0, y0, z0), (x1, y1, z1) = spec.stock_bbox()
    L, W, T = x1 - x0, y1 - y0, z1 - z0
    if is_legacy(spec) if legacy is None else legacy:
        return {
            "top": Frame("top", _v(0, 0, z1), X, Y, Z, (x0, x1), (y0, y1), T),
            "bottom": Frame("bottom", _v(0, 0, z0), X, Y, -Z, (x0, x1), (y0, y1), T),
            "front": Frame("front", _v(0, y0, 0), X, Z, -Y, (x0, x1), (z0, z1), W),
            "back": Frame("back", _v(0, y1, 0), X, Z, Y, (x0, x1), (z0, z1), W),
            "left": Frame("left", _v(x0, 0, 0), Y, Z, -X, (y0, y1), (z0, z1), L),
            "right": Frame("right", _v(x1, 0, 0), Y, Z, X, (y0, y1), (z0, z1), L),
        }
    return {
        "top": Frame("top", _v(0, 0, z1), X, Y, Z, (x0, x1), (y0, y1), T),
        "bottom": Frame("bottom", _v(x0, y1, z0), X, -Y, -Z, (0, L), (0, W), T),
        "front": Frame("front", _v(x0, y0, z0), X, Z, -Y, (0, L), (0, T), W),
        "back": Frame("back", _v(x1, y1, z0), -X, Z, Y, (0, L), (0, T), W),
        "left": Frame("left", _v(x0, y1, z0), -Y, Z, -X, (0, W), (0, T), L),
        "right": Frame("right", _v(x1, y0, z0), Y, Z, X, (0, W), (0, T), L),
    }


def plane_frame(spec: "PartSpec", plane) -> Frame:
    R = rotation_spatial_xyz(plane.rot_a_deg, plane.rot_b_deg, plane.rot_c_deg)
    (x0, y0, z0), (x1, y1, z1) = spec.stock_bbox()
    diag = math.dist((x0, y0, z0), (x1, y1, z1))
    o = _v(plane.origin_x, plane.origin_y, plane.origin_z)
    thick = _ray_in_box(o, -R[:, 2], _v(x0, y0, z0), _v(x1, y1, z1)) or diag
    return Frame(f"{PLANE_PREFIX}{plane.id}", o, R[:, 0], R[:, 1], R[:, 2], (-diag, diag), (-diag, diag),
                 thick, tilted=True)


def _ray_in_box(o: np.ndarray, d: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> float:
    """Length of the ray o + t d (t >= 0) inside the box (slab method); 0 when it misses."""
    t0, t1 = 0.0, math.inf
    for i in range(3):
        if abs(d[i]) < 1e-12:
            if not lo[i] - 1e-9 <= o[i] <= hi[i] + 1e-9:
                return 0.0
            continue
        a, b = (lo[i] - o[i]) / d[i], (hi[i] - o[i]) / d[i]
        t0, t1 = max(t0, min(a, b)), min(t1, max(a, b))
    return max(t1 - t0, 0.0) if t1 < math.inf else 0.0


def frames(spec: "PartSpec") -> dict[str, Frame]:
    """All frames of a spec: the six stock faces + one per tilted plane (key ``plane:<id>``)."""
    out = ortho_frames(spec)
    for p in getattr(spec, "tilted_planes", []) or []:
        out[f"{PLANE_PREFIX}{p.id}"] = plane_frame(spec, p)
    return out
