"""Multi-body glTF previews with per-body materials (softjaw assembly, revision diff).

Same conventions as verifier.export_glb: face-by-face tessellation (sharp CAD edges), Z-up model converted to the
glTF Y-up frame (the viewer rotates back). Each layer becomes one named node with a PBR material, so the viewer can
toggle layers by name and keep translucency (alphaMode BLEND).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

Y_UP = np.array([[1, 0, 0, 0], [0, 0, 1, 0], [0, -1, 0, 0], [0, 0, 0, 1]], dtype=float)


@dataclass
class Layer:
    name: str
    shapes: list  # build123d shapes (solids / compounds)
    rgb: tuple[int, int, int]
    opacity: float = 1.0


def shape_mesh(shape, tolerance: float = 0.02, angular_tolerance: float = 0.15):
    import trimesh

    tol = max(tolerance, shape.bounding_box().diagonal / 4000.0)
    parts = []
    for face in shape.faces():
        verts, tris = face.tessellate(tol, angular_tolerance)
        if not tris:
            continue
        m = trimesh.Trimesh(vertices=np.array([[p.X, p.Y, p.Z] for p in verts], dtype=np.float64),
                            faces=np.array(tris, dtype=np.int64), process=False)
        m.merge_vertices()
        parts.append(m)
    return trimesh.util.concatenate(parts) if parts else None


def export_layers_glb(layers: list[Layer], path: Path) -> Path:
    import trimesh
    from trimesh.visual.material import PBRMaterial

    scene = trimesh.Scene()
    for layer in layers:
        meshes = [m for m in (shape_mesh(s) for s in layer.shapes) if m is not None]
        if not meshes:
            continue
        mesh = trimesh.util.concatenate(meshes)
        mesh.apply_transform(Y_UP)
        alpha = max(0.0, min(1.0, layer.opacity))
        mat = PBRMaterial(name=layer.name, baseColorFactor=[*(c / 255 for c in layer.rgb), alpha],
                          metallicFactor=0.15, roughnessFactor=0.55, doubleSided=True,
                          alphaMode="BLEND" if alpha < 1 else "OPAQUE")
        mesh.visual = trimesh.visual.TextureVisuals(material=mat)
        _ = mesh.vertex_normals
        scene.add_geometry(mesh, node_name=layer.name, geom_name=layer.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(trimesh.exchange.gltf.export_glb(scene, include_normals=True))
    return path
