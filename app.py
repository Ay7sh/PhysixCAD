from __future__ import annotations

import json
import math
import re
import ssl
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from source_pipeline import run_pipeline

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DATABASE_PATH = ROOT / "database.json"

app = FastAPI(
    title="PhysixCAD MVP API",
    description="Smart CAD marketplace prototype with physics metadata packages.",
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def load_database() -> dict[str, Any]:
    with DATABASE_PATH.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def list_parts() -> list[dict[str, Any]]:
    return load_database()["parts"]


def get_part(part_id: str) -> dict[str, Any]:
    for part in list_parts():
        if part["id"] == part_id:
            return part
    raise HTTPException(status_code=404, detail=f"Unknown part id: {part_id}")


def safe_package_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-").lower()


def fetch_remote_asset(url: str, timeout_seconds: float = 15.0) -> tuple[bytes, dict[str, Any]]:
    request = Request(
        url,
        headers={
            "User-Agent": "PhysixCAD-MVP/1.0 (+https://physixcad.local)",
            "Accept": "application/octet-stream,text/plain,application/zip,*/*",
        },
    )
    started = time.monotonic()
    used_certificate_fallback = False
    try:
        response_context = urlopen(request, timeout=timeout_seconds)
    except URLError as exc:
        if not isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise
        used_certificate_fallback = True
        response_context = urlopen(
            request,
            timeout=timeout_seconds,
            context=ssl._create_unverified_context(),
        )

    with response_context as response:
        data = response.read()
        status = response.status
        content_type = response.headers.get("content-type")

    if not looks_like_cad_asset(url, content_type, data):
        raise ValueError(f"Remote URL did not return a CAD asset; content-type={content_type!r}")

    return data, {
        "ok": True,
        "status": status,
        "content_type": content_type,
        "content_length": len(data),
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "certificate_verification_fallback": used_certificate_fallback,
    }


def looks_like_cad_asset(url: str, content_type: str | None, data: bytes) -> bool:
    lower_url = url.lower().split("?", 1)[0]
    lower_type = (content_type or "").lower()
    if "text/html" in lower_type or data[:100].lstrip().lower().startswith(b"<!doctype html"):
        return False
    if lower_url.endswith((".step", ".stp", ".stl", ".obj", ".3mf", ".zip")):
        return True
    return any(
        token in lower_type
        for token in (
            "model/stl",
            "application/sla",
            "application/zip",
            "application/octet-stream",
            "text/plain",
        )
    )


def box_mesh_stl(name: str, dimensions_mm: tuple[float, float, float]) -> str:
    """Generate a tiny fallback STL preview when a remote CAD host is unavailable."""
    x, y, z = [d / 2.0 for d in dimensions_mm]
    vertices = [
        (-x, -y, -z),
        (x, -y, -z),
        (x, y, -z),
        (-x, y, -z),
        (-x, -y, z),
        (x, -y, z),
        (x, y, z),
        (-x, y, z),
    ]
    faces = [
        (0, 1, 2),
        (0, 2, 3),
        (4, 6, 5),
        (4, 7, 6),
        (0, 4, 5),
        (0, 5, 1),
        (1, 5, 6),
        (1, 6, 2),
        (2, 6, 7),
        (2, 7, 3),
        (3, 7, 4),
        (3, 4, 0),
    ]

    def normal(a: tuple[float, float, float], b: tuple[float, float, float], c: tuple[float, float, float]) -> tuple[float, float, float]:
        ux, uy, uz = (b[i] - a[i] for i in range(3))
        vx, vy, vz = (c[i] - a[i] for i in range(3))
        nx, ny, nz = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
        length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        return (nx / length, ny / length, nz / length)

    lines = [f"solid {safe_package_name(name)}"]
    for face in faces:
        a, b, c = (vertices[index] for index in face)
        nx, ny, nz = normal(a, b, c)
        lines.append(f"  facet normal {nx:.6f} {ny:.6f} {nz:.6f}")
        lines.append("    outer loop")
        for vertex in (a, b, c):
            lines.append(f"      vertex {vertex[0]:.6f} {vertex[1]:.6f} {vertex[2]:.6f}")
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append(f"endsolid {safe_package_name(name)}")
    return "\n".join(lines) + "\n"


def triangle_normal(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    c: tuple[float, float, float],
) -> tuple[float, float, float]:
    ux, uy, uz = (b[i] - a[i] for i in range(3))
    vx, vy, vz = (c[i] - a[i] for i in range(3))
    nx, ny, nz = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
    length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
    return (nx / length, ny / length, nz / length)


def mesh_stl(name: str, vertices: list[tuple[float, float, float]], faces: list[tuple[int, int, int]]) -> str:
    lines = [f"solid {safe_package_name(name)}"]
    for face in faces:
        a, b, c = (vertices[index] for index in face)
        nx, ny, nz = triangle_normal(a, b, c)
        lines.append(f"  facet normal {nx:.6f} {ny:.6f} {nz:.6f}")
        lines.append("    outer loop")
        for vertex in (a, b, c):
            lines.append(f"      vertex {vertex[0]:.6f} {vertex[1]:.6f} {vertex[2]:.6f}")
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append(f"endsolid {safe_package_name(name)}")
    return "\n".join(lines) + "\n"


def box_mesh(name: str, dimensions_mm: tuple[float, float, float], center: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    x, y, z = [d / 2.0 for d in dimensions_mm]
    cx, cy, cz = center
    vertices = [
        (cx - x, cy - y, cz - z),
        (cx + x, cy - y, cz - z),
        (cx + x, cy + y, cz - z),
        (cx - x, cy + y, cz - z),
        (cx - x, cy - y, cz + z),
        (cx + x, cy - y, cz + z),
        (cx + x, cy + y, cz + z),
        (cx - x, cy + y, cz + z),
    ]
    faces = [
        (0, 1, 2), (0, 2, 3),
        (4, 6, 5), (4, 7, 6),
        (0, 4, 5), (0, 5, 1),
        (1, 5, 6), (1, 6, 2),
        (2, 6, 7), (2, 7, 3),
        (3, 7, 4), (3, 4, 0),
    ]
    return vertices, faces


def multi_box_stl(name: str, boxes: list[tuple[tuple[float, float, float], tuple[float, float, float]]]) -> str:
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    for dimensions, center in boxes:
        box_vertices, box_faces = box_mesh(name, dimensions, center)
        offset = len(vertices)
        vertices.extend(box_vertices)
        faces.extend((a + offset, b + offset, c + offset) for a, b, c in box_faces)
    return mesh_stl(name, vertices, faces)


def cylinder_mesh_stl(name: str, radius_mm: float, length_mm: float, segments: int = 32) -> str:
    segments = max(12, min(96, int(segments)))
    half = length_mm / 2.0
    vertices: list[tuple[float, float, float]] = []
    for z in (-half, half):
        for index in range(segments):
            angle = 2.0 * math.pi * index / segments
            vertices.append((math.cos(angle) * radius_mm, math.sin(angle) * radius_mm, z))
    bottom_center = len(vertices)
    vertices.append((0.0, 0.0, -half))
    top_center = len(vertices)
    vertices.append((0.0, 0.0, half))
    faces: list[tuple[int, int, int]] = []
    for index in range(segments):
        nxt = (index + 1) % segments
        b0, b1 = index, nxt
        t0, t1 = index + segments, nxt + segments
        faces.append((b0, b1, t1))
        faces.append((b0, t1, t0))
        faces.append((bottom_center, b1, b0))
        faces.append((top_center, t0, t1))
    return mesh_stl(name, vertices, faces)


def gear_mesh_stl(name: str, root_radius_mm: float, outer_radius_mm: float, thickness_mm: float, teeth: int) -> str:
    points = max(24, min(160, int(teeth) * 2))
    half = thickness_mm / 2.0
    vertices: list[tuple[float, float, float]] = []
    for z in (-half, half):
        for index in range(points):
            angle = 2.0 * math.pi * index / points
            radius = outer_radius_mm if index % 2 == 0 else root_radius_mm
            vertices.append((math.cos(angle) * radius, math.sin(angle) * radius, z))
    bottom_center = len(vertices)
    vertices.append((0.0, 0.0, -half))
    top_center = len(vertices)
    vertices.append((0.0, 0.0, half))
    faces: list[tuple[int, int, int]] = []
    for index in range(points):
        nxt = (index + 1) % points
        b0, b1 = index, nxt
        t0, t1 = index + points, nxt + points
        faces.append((b0, b1, t1))
        faces.append((b0, t1, t0))
        faces.append((bottom_center, b1, b0))
        faces.append((top_center, t0, t1))
    return mesh_stl(name, vertices, faces)


def procedural_stl(part: dict[str, Any]) -> bytes:
    parameters = part["cad"].get("parameters", {})
    shape = parameters.get("shape")
    if shape == "cylinder":
        text = cylinder_mesh_stl(
            part["name"],
            float(parameters["radius_mm"]),
            float(parameters["length_mm"]),
            int(parameters.get("segments", 32)),
        )
    elif shape == "gear":
        text = gear_mesh_stl(
            part["name"],
            float(parameters["root_radius_mm"]),
            float(parameters["outer_radius_mm"]),
            float(parameters["thickness_mm"]),
            int(parameters["teeth"]),
        )
    elif shape == "bracket":
        width = float(parameters["width_mm"])
        height = float(parameters["height_mm"])
        depth = float(parameters["depth_mm"])
        thickness = float(parameters["thickness_mm"])
        text = multi_box_stl(
            part["name"],
            [
                ((width, depth, thickness), (0.0, 0.0, thickness / 2.0)),
                ((thickness, depth, height), (-width / 2.0 + thickness / 2.0, 0.0, height / 2.0)),
            ],
        )
    else:
        text = box_mesh_stl(
            part["name"],
            (
                float(parameters.get("width_mm", 20.0)),
                float(parameters.get("depth_mm", 20.0)),
                float(parameters.get("height_mm", 10.0)),
            ),
        )
    return text.encode("utf-8")


def fallback_dimensions(part: dict[str, Any]) -> tuple[float, float, float]:
    part_id = part["id"]
    if "bearing" in part_id:
        return (22.0, 22.0, 7.0)
    if "sg90" in part_id:
        return (23.0, 12.2, 29.0)
    if "extrusion" in part_id:
        return (20.0, 20.0, 100.0)
    if "2207" in part_id:
        return (28.0, 28.0, 32.0)
    return (42.0, 42.0, 40.0)


def build_smart_package(part: dict[str, Any]) -> BytesIO:
    part_slug = safe_package_name(part["id"])
    package = BytesIO()
    manifest: dict[str, Any] = {
        "package": f"{part_slug}-smart-cad",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "part_id": part["id"],
        "part_name": part["name"],
        "source": part["cad"],
        "cad_download": None,
        "contents": [
            "physics/metadata.json",
            "manifest.json",
            "README.txt",
        ],
    }

    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "physics/metadata.json",
            json.dumps(
                {
                    "id": part["id"],
                    "name": part["name"],
                    "category": part["category"],
                    "physics": part["physics"],
                    "cad": part["cad"],
                    "media": part.get("media"),
                    "competition_relevance": part.get("competition_relevance", []),
                    "metadata_quality": part.get("metadata_quality"),
                },
                indent=2,
            )
            + "\n",
        )

        try:
            if part["cad"].get("source_type") == "procedural":
                cad_bytes = procedural_stl(part)
                remote_status = {
                    "ok": True,
                    "status": "generated",
                    "source_type": "procedural",
                    "generator": part["cad"].get("generator"),
                    "content_type": "model/stl",
                    "content_length": len(cad_bytes),
                }
            else:
                cad_bytes, remote_status = fetch_remote_asset(part["cad"]["download_url"])
            cad_path = f"cad/{part['cad']['filename']}"
            archive.writestr(cad_path, cad_bytes)
            manifest["cad_download"] = remote_status
            manifest["contents"].append(cad_path)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            fallback_filename = f"{part_slug}-fallback-preview.stl"
            fallback_path = f"cad/{fallback_filename}"
            archive.writestr(
                fallback_path,
                box_mesh_stl(part["name"], fallback_dimensions(part)),
            )
            manifest["cad_download"] = {
                "ok": False,
                "error": str(exc),
                "fallback": fallback_filename,
            }
            manifest["contents"].append(fallback_path)

        archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        archive.writestr(
            "README.txt",
            (
                f"PhysixCAD Smart CAD Package\n"
                f"Part: {part['name']}\n"
                f"Source: {part['cad']['source_page']}\n"
                f"CAD URL: {part['cad']['download_url']}\n\n"
                "Import the CAD file into your geometry tool, then map physics/metadata.json "
                "into Unity, Unreal, MATLAB Simscape, or your own simulation pipeline.\n"
            ),
        )

    package.seek(0)
    return package


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "PhysixCAD MVP API"}


@app.get("/api/parts")
def api_parts() -> dict[str, Any]:
    data = load_database()
    parts = data["parts"]
    return {
        "count": len(parts),
        "catalog": data.get("catalog", {}),
        "categories": sorted({part["category"] for part in parts}),
        "parts": parts,
    }


@app.get("/api/parts/{part_id}")
def api_part(part_id: str) -> dict[str, Any]:
    return get_part(part_id)


@app.get("/api/parts/{part_id}/cad")
def api_part_cad(part_id: str):
    part = get_part(part_id)
    if part["cad"].get("source_type") == "procedural":
        return StreamingResponse(
            BytesIO(procedural_stl(part)),
            media_type="model/stl",
            headers={"Content-Disposition": f'attachment; filename="{part["cad"]["filename"]}"'},
        )
    return RedirectResponse(part["cad"]["download_url"])


@app.get("/api/source-pipeline")
def api_source_pipeline(
    validate: bool = Query(False, description="Validate CAD URLs with remote requests."),
    limit: int | None = Query(None, ge=1, le=250, description="Limit the number of sources emitted or validated."),
) -> JSONResponse:
    return JSONResponse(run_pipeline(validate=validate, limit=limit))


@app.get("/api/parts/{part_id}/smart-package")
def api_smart_package(part_id: str) -> StreamingResponse:
    part = get_part(part_id)
    package = build_smart_package(part)
    filename = f"{safe_package_name(part['id'])}-smart-cad.zip"
    return StreamingResponse(
        package,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
