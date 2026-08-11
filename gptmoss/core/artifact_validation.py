"""Dependency-free, extensible validation of generated artifacts."""

from __future__ import annotations

import hashlib
import json
import math
import struct
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

from gptmoss.core.document_quality import validate_document


ValidationReport = Dict[str, Any]
Validator = Callable[[Path, Dict[str, Any]], ValidationReport]
_VALIDATORS: Dict[str, Validator] = {}


def register_artifact_validator(name: str, validator: Validator) -> None:
    """Register a project/domain validator without coupling it to GPTMOSS."""
    normalized = str(name or "").strip().lower().lstrip(".")
    if not normalized:
        raise ValueError("Validator name cannot be empty.")
    _VALIDATORS[normalized] = validator


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _failure(report: ValidationReport, message: str) -> None:
    report["valid"] = False
    report.setdefault("failures", []).append(message)


def _apply_numeric_constraints(
    report: ValidationReport, metrics: Dict[str, Any], constraints: Dict[str, Any]
) -> None:
    for metric, minimum in (constraints.get("minimums") or {}).items():
        value = metrics.get(metric)
        if not isinstance(value, (int, float)) or value < minimum:
            _failure(report, f"{metric}={value!r} is below required minimum {minimum!r}")
    for metric, maximum in (constraints.get("maximums") or {}).items():
        value = metrics.get(metric)
        if not isinstance(value, (int, float)) or value > maximum:
            _failure(report, f"{metric}={value!r} exceeds required maximum {maximum!r}")


def _resolve_obj_index(raw: str, count: int) -> Optional[int]:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value == 0:
        return None
    resolved = value - 1 if value > 0 else count + value
    return resolved if 0 <= resolved < count else None


def _triangle_area(a: Iterable[float], b: Iterable[float], c: Iterable[float]) -> float:
    ax, ay, az = a
    bx, by, bz = b
    cx, cy, cz = c
    ux, uy, uz = bx - ax, by - ay, bz - az
    vx, vy, vz = cx - ax, cy - ay, cz - az
    cross = (
        uy * vz - uz * vy,
        uz * vx - ux * vz,
        ux * vy - uy * vx,
    )
    return math.sqrt(sum(value * value for value in cross)) * 0.5


def validate_obj(path: Path, constraints: Dict[str, Any]) -> ValidationReport:
    report: ValidationReport = {"validator": "obj", "valid": True, "failures": [], "warnings": []}
    vertices = []
    normals = []
    texcoords = []
    faces = []
    invalid_numbers = 0
    invalid_indices = 0
    try:
        with path.open("r", encoding="utf-8", errors="strict") as source:
            for line_number, line in enumerate(source, 1):
                parts = line.strip().split()
                if not parts or parts[0].startswith("#"):
                    continue
                kind, values = parts[0], parts[1:]
                if kind in {"v", "vn"}:
                    if len(values) < 3:
                        invalid_numbers += 1
                        continue
                    try:
                        point = tuple(float(value) for value in values[:3])
                    except ValueError:
                        invalid_numbers += 1
                        continue
                    if not all(math.isfinite(value) for value in point):
                        invalid_numbers += 1
                    (vertices if kind == "v" else normals).append(point)
                elif kind == "vt":
                    try:
                        uv = tuple(float(value) for value in values[:2])
                    except ValueError:
                        invalid_numbers += 1
                        continue
                    if len(uv) < 2 or not all(math.isfinite(value) for value in uv):
                        invalid_numbers += 1
                    texcoords.append(uv)
                elif kind == "f":
                    if len(values) < 3:
                        invalid_indices += 1
                        continue
                    indices = []
                    for token in values:
                        components = token.split("/")
                        vertex_index = _resolve_obj_index(components[0], len(vertices))
                        if vertex_index is None:
                            invalid_indices += 1
                            break
                        if len(components) > 1 and components[1]:
                            if _resolve_obj_index(components[1], len(texcoords)) is None:
                                invalid_indices += 1
                                break
                        if len(components) > 2 and components[2]:
                            if _resolve_obj_index(components[2], len(normals)) is None:
                                invalid_indices += 1
                                break
                        indices.append(vertex_index)
                    else:
                        faces.append((line_number, indices))
    except (OSError, UnicodeError) as error:
        _failure(report, f"cannot read OBJ: {error}")
        return report

    triangles = []
    for _, face in faces:
        triangles.extend((face[0], face[index], face[index + 1]) for index in range(1, len(face) - 1))
    degenerate = sum(
        1 for a, b, c in triangles
        if len({a, b, c}) < 3 or _triangle_area(vertices[a], vertices[b], vertices[c]) <= 1e-12
    )
    edges = Counter(
        tuple(sorted(edge))
        for triangle in triangles
        for edge in ((triangle[0], triangle[1]), (triangle[1], triangle[2]), (triangle[2], triangle[0]))
    )
    bounds = None
    extents = None
    if vertices:
        minimum = [min(point[axis] for point in vertices) for axis in range(3)]
        maximum = [max(point[axis] for point in vertices) for axis in range(3)]
        bounds = {"minimum": minimum, "maximum": maximum}
        extents = [maximum[axis] - minimum[axis] for axis in range(3)]
    non_unit_normals = sum(
        1 for normal in normals
        if not math.isclose(math.sqrt(sum(value * value for value in normal)), 1.0, rel_tol=1e-3, abs_tol=1e-3)
    )
    metrics = {
        "vertices": len(vertices),
        "normals": len(normals),
        "texcoords": len(texcoords),
        "faces": len(faces),
        "triangles": len(triangles),
        "degenerate_faces": degenerate,
        "boundary_edges": sum(1 for count in edges.values() if count == 1),
        "nonmanifold_edges": sum(1 for count in edges.values() if count > 2),
        "invalid_numbers": invalid_numbers,
        "invalid_indices": invalid_indices,
        "non_unit_normals": non_unit_normals,
        "bounds": bounds,
        "extents": extents,
    }
    report["metrics"] = metrics
    if not vertices:
        _failure(report, "OBJ has no vertices")
    if not faces:
        _failure(report, "OBJ has no faces")
    if invalid_numbers:
        _failure(report, f"OBJ contains {invalid_numbers} malformed or non-finite numeric value(s)")
    if invalid_indices:
        _failure(report, f"OBJ contains {invalid_indices} invalid face index reference(s)")
    if degenerate > int(constraints.get("max_degenerate_faces", 0)):
        _failure(report, f"OBJ contains {degenerate} degenerate triangle(s)")
    if constraints.get("closed_mesh") and metrics["boundary_edges"]:
        _failure(report, f"OBJ mesh is open ({metrics['boundary_edges']} boundary edges)")
    extent_constraints = constraints.get("extents") or {}
    if extents and isinstance(extent_constraints, dict):
        for axis, index in {"x": 0, "y": 1, "z": 2}.items():
            limits = extent_constraints.get(axis)
            if not isinstance(limits, list) or len(limits) != 2:
                continue
            if not limits[0] <= extents[index] <= limits[1]:
                _failure(
                    report,
                    f"OBJ {axis} extent {extents[index]} is outside [{limits[0]}, {limits[1]}]",
                )
    if metrics["nonmanifold_edges"]:
        _failure(report, f"OBJ has {metrics['nonmanifold_edges']} non-manifold edge(s)")
    if non_unit_normals:
        report["warnings"].append(f"{non_unit_normals} normal(s) are not unit length")
    _apply_numeric_constraints(report, metrics, constraints)
    return report


def validate_glb(path: Path, constraints: Dict[str, Any]) -> ValidationReport:
    report: ValidationReport = {"validator": "glb", "valid": True, "failures": [], "warnings": []}
    try:
        data = path.read_bytes()
    except OSError as error:
        _failure(report, f"cannot read GLB: {error}")
        return report
    if len(data) < 20:
        _failure(report, "GLB is shorter than its mandatory header and JSON chunk")
        return report
    magic, version, declared_length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF":
        _failure(report, "invalid GLB magic")
    if version != 2:
        _failure(report, f"unsupported GLB version {version}")
    if declared_length != len(data):
        _failure(report, f"declared GLB length {declared_length} differs from file size {len(data)}")
    offset = 12
    chunks = []
    while offset + 8 <= len(data):
        chunk_length, chunk_type = struct.unpack_from("<II", data, offset)
        offset += 8
        end = offset + chunk_length
        if end > len(data):
            _failure(report, "GLB chunk exceeds declared file boundary")
            break
        chunks.append((chunk_type, data[offset:end]))
        offset = end
    if offset != len(data):
        _failure(report, "GLB has trailing or truncated chunk bytes")
    json_chunks = [chunk for kind, chunk in chunks if kind == 0x4E4F534A]
    if not json_chunks:
        _failure(report, "GLB contains no JSON chunk")
        document = {}
    else:
        try:
            document = json.loads(json_chunks[0].rstrip(b" \t\r\n\x00").decode("utf-8"))
        except (UnicodeError, ValueError) as error:
            document = {}
            _failure(report, f"invalid GLB JSON document: {error}")
    asset_version = str((document.get("asset") or {}).get("version") or "")
    if document and asset_version != "2.0":
        _failure(report, f"glTF asset.version is {asset_version!r}, expected '2.0'")
    binary_chunks = [chunk for kind, chunk in chunks if kind == 0x004E4942]
    binary = binary_chunks[0] if binary_chunks else b""
    reference_failures, non_finite_values = _validate_gltf_document(
        document, binary
    )
    metrics = {
        "chunks": len(chunks),
        "meshes": len(document.get("meshes") or []),
        "nodes": len(document.get("nodes") or []),
        "skins": len(document.get("skins") or []),
        "animations": len(document.get("animations") or []),
        "materials": len(document.get("materials") or []),
        "accessors": len(document.get("accessors") or []),
        "buffer_views": len(document.get("bufferViews") or []),
        "buffers": len(document.get("buffers") or []),
        "invalid_references": len(reference_failures),
        "non_finite_values": non_finite_values,
    }
    report["metrics"] = metrics
    if constraints.get("require_mesh") and not metrics["meshes"]:
        _failure(report, "GLB contains no mesh")
    if constraints.get("require_skin") and not metrics["skins"]:
        _failure(report, "GLB contains no skin")
    for failure in reference_failures:
        _failure(report, failure)
    if non_finite_values:
        _failure(report, f"GLB contains {non_finite_values} non-finite accessor value(s)")
    _apply_numeric_constraints(report, metrics, constraints)
    return report


_COMPONENT_FORMATS = {
    5120: ("b", 1),
    5121: ("B", 1),
    5122: ("h", 2),
    5123: ("H", 2),
    5125: ("I", 4),
    5126: ("f", 4),
}
_TYPE_COMPONENTS = {
    "SCALAR": 1,
    "VEC2": 2,
    "VEC3": 3,
    "VEC4": 4,
    "MAT2": 4,
    "MAT3": 9,
    "MAT4": 16,
}


def _reference(
    collection: list, value: Any, label: str, failures: list[str]
) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < len(collection):
        failures.append(f"{label} references invalid index {value!r}")
        return None
    return value


def _validate_gltf_document(document: Dict[str, Any], binary: bytes) -> tuple[list[str], int]:
    """Validate GLB graph references and decode finite float accessors."""
    if not isinstance(document, dict):
        return ["glTF JSON root is not an object"], 0
    failures: list[str] = []
    buffers = document.get("buffers") or []
    views = document.get("bufferViews") or []
    accessors = document.get("accessors") or []
    meshes = document.get("meshes") or []
    nodes = document.get("nodes") or []
    skins = document.get("skins") or []
    scenes = document.get("scenes") or []
    materials = document.get("materials") or []
    animations = document.get("animations") or []
    collections = {
        "buffers": buffers,
        "bufferViews": views,
        "accessors": accessors,
        "meshes": meshes,
        "nodes": nodes,
        "skins": skins,
        "scenes": scenes,
        "materials": materials,
        "animations": animations,
    }
    for name, collection in collections.items():
        if not isinstance(collection, list):
            failures.append(f"glTF {name} must be an array")
            collections[name] = []
    buffers, views, accessors = (
        collections["buffers"], collections["bufferViews"], collections["accessors"]
    )
    meshes, nodes, skins = collections["meshes"], collections["nodes"], collections["skins"]
    scenes, materials = collections["scenes"], collections["materials"]
    if buffers:
        declared = buffers[0].get("byteLength") if isinstance(buffers[0], dict) else None
        if not isinstance(declared, int) or declared < 0 or declared > len(binary):
            failures.append(
                f"buffer 0 byteLength {declared!r} exceeds binary chunk size {len(binary)}"
            )

    for index, view in enumerate(views):
        if not isinstance(view, dict):
            failures.append(f"bufferView {index} is not an object")
            continue
        buffer_index = _reference(buffers, view.get("buffer", 0), f"bufferView {index}", failures)
        offset = view.get("byteOffset", 0)
        length = view.get("byteLength")
        if (
            buffer_index is None or not isinstance(offset, int) or offset < 0
            or not isinstance(length, int) or length < 0
        ):
            failures.append(f"bufferView {index} has invalid byte range")
            continue
        available = len(binary) if buffer_index == 0 else int(buffers[buffer_index].get("byteLength", 0))
        if offset + length > available:
            failures.append(f"bufferView {index} exceeds buffer boundary")

    non_finite = 0
    for index, accessor in enumerate(accessors):
        if not isinstance(accessor, dict):
            failures.append(f"accessor {index} is not an object")
            continue
        component_type = accessor.get("componentType")
        accessor_type = accessor.get("type")
        count = accessor.get("count")
        if component_type not in _COMPONENT_FORMATS or accessor_type not in _TYPE_COMPONENTS:
            failures.append(f"accessor {index} has invalid componentType or type")
            continue
        if not isinstance(count, int) or count < 0:
            failures.append(f"accessor {index} has invalid count {count!r}")
            continue
        for key in ("min", "max"):
            values = accessor.get(key)
            if values is not None and (
                not isinstance(values, list)
                or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values)
            ):
                failures.append(f"accessor {index} has invalid {key} bounds")
        view_value = accessor.get("bufferView")
        if view_value is None:
            continue
        view_index = _reference(views, view_value, f"accessor {index}", failures)
        if view_index is None or not isinstance(views[view_index], dict):
            continue
        view = views[view_index]
        _, component_size = _COMPONENT_FORMATS[component_type]
        components = _TYPE_COMPONENTS[accessor_type]
        element_size = component_size * components
        stride = view.get("byteStride", element_size)
        accessor_offset = accessor.get("byteOffset", 0)
        view_offset = view.get("byteOffset", 0)
        if (
            not isinstance(stride, int) or stride < element_size
            or not isinstance(accessor_offset, int) or accessor_offset < 0
        ):
            failures.append(f"accessor {index} has invalid offset or stride")
            continue
        required = accessor_offset + (count - 1) * stride + element_size if count else accessor_offset
        if required > int(view.get("byteLength", 0)):
            failures.append(f"accessor {index} exceeds its bufferView boundary")
            continue
        if component_type == 5126 and binary:
            start = int(view_offset) + accessor_offset
            for item_index in range(count):
                item_offset = start + item_index * stride
                try:
                    values = struct.unpack_from("<" + "f" * components, binary, item_offset)
                except struct.error:
                    failures.append(f"accessor {index} cannot be decoded from binary chunk")
                    break
                non_finite += sum(not math.isfinite(value) for value in values)

    for mesh_index, mesh in enumerate(meshes):
        primitives = mesh.get("primitives") if isinstance(mesh, dict) else None
        if not isinstance(primitives, list):
            failures.append(f"mesh {mesh_index} has no primitives array")
            continue
        for primitive_index, primitive in enumerate(primitives):
            if not isinstance(primitive, dict):
                failures.append(f"mesh {mesh_index} primitive {primitive_index} is not an object")
                continue
            attributes = primitive.get("attributes")
            if not isinstance(attributes, dict) or "POSITION" not in attributes:
                failures.append(f"mesh {mesh_index} primitive {primitive_index} has no POSITION accessor")
            else:
                for semantic, accessor_index in attributes.items():
                    _reference(
                        accessors, accessor_index,
                        f"mesh {mesh_index} primitive {primitive_index} attribute {semantic}",
                        failures,
                    )
            if "indices" in primitive:
                index = _reference(
                    accessors, primitive["indices"],
                    f"mesh {mesh_index} primitive {primitive_index} indices", failures,
                )
                if index is not None and isinstance(accessors[index], dict):
                    accessor = accessors[index]
                    if accessor.get("type") != "SCALAR" or accessor.get("componentType") not in (5121, 5123, 5125):
                        failures.append(
                            f"mesh {mesh_index} primitive {primitive_index} uses an invalid index accessor"
                        )
            if "material" in primitive:
                _reference(materials, primitive["material"], f"mesh {mesh_index} primitive", failures)

    child_graph = {}
    for node_index, node in enumerate(nodes):
        if not isinstance(node, dict):
            failures.append(f"node {node_index} is not an object")
            continue
        children = node.get("children") or []
        if not isinstance(children, list):
            failures.append(f"node {node_index} children must be an array")
            children = []
        valid_children = []
        for child in children:
            reference = _reference(nodes, child, f"node {node_index} child", failures)
            if reference is not None:
                valid_children.append(reference)
        child_graph[node_index] = valid_children
        if "mesh" in node:
            _reference(meshes, node["mesh"], f"node {node_index} mesh", failures)
        if "skin" in node:
            _reference(skins, node["skin"], f"node {node_index} skin", failures)
        for key, expected in (("matrix", 16), ("translation", 3), ("rotation", 4), ("scale", 3)):
            values = node.get(key)
            if values is not None and (
                not isinstance(values, list) or len(values) != expected
                or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values)
            ):
                failures.append(f"node {node_index} has invalid {key}")

    def visit(node_index: int, ancestry: set[int]) -> None:
        if node_index in ancestry:
            failures.append(f"node hierarchy contains a cycle at node {node_index}")
            return
        for child in child_graph.get(node_index, []):
            visit(child, ancestry | {node_index})

    for node_index in child_graph:
        visit(node_index, set())

    for skin_index, skin in enumerate(skins):
        joints = skin.get("joints") if isinstance(skin, dict) else None
        if not isinstance(joints, list) or not joints:
            failures.append(f"skin {skin_index} has no joints")
            continue
        for joint in joints:
            _reference(nodes, joint, f"skin {skin_index} joint", failures)
        if "inverseBindMatrices" in skin:
            accessor_index = _reference(
                accessors, skin["inverseBindMatrices"],
                f"skin {skin_index} inverseBindMatrices", failures,
            )
            if accessor_index is not None and isinstance(accessors[accessor_index], dict):
                accessor = accessors[accessor_index]
                if accessor.get("type") != "MAT4" or int(accessor.get("count", -1)) < len(joints):
                    failures.append(f"skin {skin_index} has incompatible inverse bind matrices")

    for scene_index, scene in enumerate(scenes):
        roots = scene.get("nodes") if isinstance(scene, dict) else None
        if not isinstance(roots, list):
            failures.append(f"scene {scene_index} nodes must be an array")
            continue
        for root in roots:
            _reference(nodes, root, f"scene {scene_index} root", failures)
    if "scene" in document:
        _reference(scenes, document["scene"], "default scene", failures)
    return failures, non_finite


def validate_json(path: Path, constraints: Dict[str, Any]) -> ValidationReport:
    report: ValidationReport = {"validator": "json", "valid": True, "failures": [], "warnings": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        _failure(report, f"invalid JSON: {error}")
        return report
    report["metrics"] = {
        "top_level_type": type(value).__name__,
        "items": len(value) if isinstance(value, (dict, list)) else 1,
    }
    expected_type = str(constraints.get("top_level_type") or "").strip().lower()
    if expected_type and report["metrics"]["top_level_type"].lower() != expected_type:
        _failure(
            report,
            f"JSON top-level type is {report['metrics']['top_level_type']}, expected {expected_type}",
        )
    _apply_numeric_constraints(report, report["metrics"], constraints)
    return report


def validate_artifact(
    path: str | Path,
    validator: Optional[str] = None,
    constraints: Optional[Dict[str, Any]] = None,
) -> ValidationReport:
    """Validate a file and dispatch to a built-in or registered validator."""
    candidate = Path(path)
    constraints = dict(constraints or {})
    report: ValidationReport = {
        "path": str(candidate),
        "valid": True,
        "failures": [],
        "warnings": [],
    }
    if not candidate.is_file():
        _failure(report, "artifact does not exist or is not a regular file")
        return report
    try:
        size = candidate.stat().st_size
        report.update({"size_bytes": size, "sha256": _sha256(candidate)})
        minimum_size = int(constraints.get("min_size_bytes", 1))
    except (OSError, TypeError, ValueError) as error:
        _failure(report, f"cannot inspect artifact or constraints: {error}")
        return report
    if size < minimum_size:
        _failure(report, f"artifact size {size} is below required minimum {minimum_size}")
        return report
    name = str(validator or candidate.suffix).strip().lower().lstrip(".")
    selected = _VALIDATORS.get(name)
    if selected:
        try:
            specialized = selected(candidate, constraints)
            if not isinstance(specialized, dict):
                raise TypeError("validator must return a report object")
            specialized.setdefault("validator", name)
            specialized.setdefault("valid", True)
            specialized.setdefault("failures", [])
            specialized.setdefault("warnings", [])
            specialized.update({
                "path": str(candidate),
                "size_bytes": size,
                "sha256": report["sha256"],
            })
        except Exception as error:
            _failure(report, f"{name} validator failed safely: {error}")
            report["validator"] = name
            report["metrics"] = {}
            return report
        return specialized
    report["validator"] = name or "binary"
    report["validation_level"] = "universal"
    report["metrics"] = {}
    if name:
        report["warnings"].append(
            f"No specialized {name!r} validator is registered; only existence, size, and SHA-256 were checked."
        )
    return report


register_artifact_validator("obj", validate_obj)
register_artifact_validator("glb", validate_glb)
register_artifact_validator("json", validate_json)
register_artifact_validator("document", validate_document)
register_artifact_validator("markdown", validate_document)
register_artifact_validator("md", validate_document)
register_artifact_validator("txt", validate_document)
