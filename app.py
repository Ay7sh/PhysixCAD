from __future__ import annotations

import json
import math
import re
import ssl
import time
import zipfile
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from source_pipeline import run_pipeline

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DATABASE_PATH = ROOT / "database.json"
VOTES_PATH = ROOT / "votes.json"
REPORTS_PATH = ROOT / "reports.json"
UPLOADS_PATH = ROOT / "uploads.json"
PRESENCE_TIMEOUT_SECONDS = 45.0

vote_lock = Lock()
report_lock = Lock()
upload_lock = Lock()
presence_lock = Lock()
active_clients: dict[str, float] = {}

COLLECTIONS = [
    {
        "id": "first-robotics-kit",
        "name": "FIRST Robotics Kit",
        "description": "Motors, bearings, structure, sensors, fasteners, and power transmission parts for student robotics teams.",
        "categories": ["Actuators & Motors", "Bearings & Linear Motion", "Structural Framing", "Sensors & Switches", "Fasteners & Hardware"],
        "keywords": ["FIRST Robotics", "Robotics Competition", "FRC", "FTC"],
        "icon": "bot",
    },
    {
        "id": "drone-racing-kit",
        "name": "Drone Racing Kit",
        "description": "Brushless motors, electronics, frames, shafts, fasteners, and lightweight parts for UAV builds.",
        "categories": ["Actuators & Motors", "Electronics & Control", "Structural Framing", "Fasteners & Hardware"],
        "keywords": ["Drone Racing", "UAV", "Aerial Robotics"],
        "icon": "send",
    },
    {
        "id": "arc-robotics-parts",
        "name": "ARC Robotics Parts",
        "description": "Competition-ready mechanisms for autonomous robotics, classroom teams, and rapid prototyping.",
        "categories": ["Robotics Assemblies", "Sensors & Switches", "Actuators & Motors", "Power Transmission"],
        "keywords": ["ARC", "Autonomous Robotics", "Robotics Competition"],
        "icon": "cpu",
    },
    {
        "id": "combat-robotics-kit",
        "name": "Combat Robotics Kit",
        "description": "Dense structural, drivetrain, weapon-support, bearing, and fastening models for combat robot design.",
        "categories": ["Structural Framing", "Power Transmission", "Bearings & Linear Motion", "Fasteners & Hardware"],
        "keywords": ["BattleBots", "Combat Robotics", "Robot Combat"],
        "icon": "shield",
    },
    {
        "id": "formula-student-kit",
        "name": "Formula Student Kit",
        "description": "Mechanical, sensor, fastener, fixture, and drivetrain assets for vehicle engineering teams.",
        "categories": ["Power Transmission", "Sensors & Switches", "Tooling & Fixtures", "Fasteners & Hardware"],
        "keywords": ["Formula Student", "SAE", "Vehicle Engineering"],
        "icon": "gauge",
    },
    {
        "id": "engineering-classroom-kit",
        "name": "Engineering Classroom Kit",
        "description": "General-purpose components for CAD lessons, physics labs, statics, controls, and simulation projects.",
        "categories": ["General Engineering", "Fasteners & Hardware", "Structural Framing", "Electronics & Control"],
        "keywords": ["Engineering Classroom", "Education", "STEM"],
        "icon": "graduation-cap",
    },
]

app = FastAPI(
    title="PhysixCAD MVP API",
    description="Smart CAD marketplace prototype with physics metadata packages.",
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class VoteRequest(BaseModel):
    voter_id: str
    vote: str


class PresenceRequest(BaseModel):
    client_id: str


class ReportRequest(BaseModel):
    reporter_id: str
    reason: str
    detail: str = ""


class UploadRequest(BaseModel):
    submitter_id: str
    name: str
    category: str
    summary: str = ""
    cad_url: str
    source_page: str = ""
    cad_format: str = "STEP"
    material: str = "User specified material"
    mass_grams: float = 100.0
    max_rpm: float = 0.0
    torque_nm: float = 0.0
    constraint_type: str = "fixed_body"
    competition_relevance: list[str] = []


def load_database() -> dict[str, Any]:
    with DATABASE_PATH.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def load_json_store(path: Path, root_key: str) -> dict[str, Any]:
    if not path.exists():
        return {root_key: [], "created_at": utc_timestamp(), "updated_at": utc_timestamp()}
    with path.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def save_json_store(path: Path, store: dict[str, Any]) -> None:
    store["updated_at"] = utc_timestamp()
    temp_path = path.with_suffix(".json.tmp")
    with temp_path.open("w", encoding="utf-8") as fp:
        json.dump(store, fp, indent=2, sort_keys=True)
        fp.write("\n")
    temp_path.replace(path)


def load_uploaded_parts() -> list[dict[str, Any]]:
    return load_json_store(UPLOADS_PATH, "uploads").get("uploads", [])


def list_parts() -> list[dict[str, Any]]:
    return [*load_database()["parts"], *load_uploaded_parts()]


def get_part(part_id: str) -> dict[str, Any]:
    for part in list_parts():
        if part["id"] == part_id:
            return part
    raise HTTPException(status_code=404, detail=f"Unknown part id: {part_id}")


def safe_package_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-").lower()


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def clean_client_id(client_id: str) -> str:
    client_id = str(client_id).strip()
    if not re.fullmatch(r"[a-zA-Z0-9._:-]{8,96}", client_id):
        raise HTTPException(status_code=400, detail="Invalid anonymous client id.")
    return client_id


def load_vote_store() -> dict[str, Any]:
    if not VOTES_PATH.exists():
        return {"parts": {}, "created_at": utc_timestamp(), "updated_at": utc_timestamp()}
    with VOTES_PATH.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def save_vote_store(store: dict[str, Any]) -> None:
    store["updated_at"] = utc_timestamp()
    temp_path = VOTES_PATH.with_suffix(".json.tmp")
    with temp_path.open("w", encoding="utf-8") as fp:
        json.dump(store, fp, indent=2, sort_keys=True)
        fp.write("\n")
    temp_path.replace(VOTES_PATH)


def summarize_vote_record(record: dict[str, Any] | None) -> dict[str, Any]:
    voters = (record or {}).get("voters", {})
    upvotes = sum(1 for vote in voters.values() if vote == "genuine")
    downvotes = sum(1 for vote in voters.values() if vote == "not_genuine")
    total = upvotes + downvotes
    score = upvotes - downvotes
    if total == 0:
        label = "Unverified"
        genuine_percent = None
    elif score >= 0:
        label = "Community says genuine"
        genuine_percent = round((upvotes / total) * 100)
    else:
        label = "Needs review"
        genuine_percent = round((upvotes / total) * 100)
    return {
        "upvotes": upvotes,
        "downvotes": downvotes,
        "score": score,
        "total": total,
        "genuine_percent": genuine_percent,
        "label": label,
        "updated_at": (record or {}).get("updated_at"),
    }


def prune_presence(now: float | None = None) -> int:
    now = now or time.monotonic()
    expired = [
        client_id
        for client_id, last_seen in active_clients.items()
        if now - last_seen > PRESENCE_TIMEOUT_SECONDS
    ]
    for client_id in expired:
        active_clients.pop(client_id, None)
    return len(active_clients)


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


def primary_material(part: dict[str, Any]) -> str:
    materials = part.get("physics", {}).get("material_composition", [])
    if materials:
        return materials[0].get("material", "Material metadata pending")
    return "Material metadata pending"


def clean_upload_text(value: str, fallback: str, max_length: int = 180) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return (value or fallback)[:max_length]


def build_uploaded_part(payload: UploadRequest) -> dict[str, Any]:
    name = clean_upload_text(payload.name, "User submitted CAD model")
    category = clean_upload_text(payload.category, "Community Uploads")
    part_id = f"user-{safe_package_name(name)}-{int(time.time())}"
    cad_format = clean_upload_text(payload.cad_format.upper(), "STEP", 12)
    mass_grams = max(0.01, round(float(payload.mass_grams), 3))
    max_rpm = max(0.0, round(float(payload.max_rpm), 2))
    torque_nm = max(0.0, round(float(payload.torque_nm), 4))
    source_page = payload.source_page.strip() or payload.cad_url.strip()
    return {
        "id": part_id,
        "name": name,
        "category": category,
        "summary": clean_upload_text(
            payload.summary,
            "Community-submitted Smart CAD model awaiting engineering review.",
            320,
        ),
        "cad": {
            "format": cad_format,
            "filename": f"{safe_package_name(name)}.{cad_format.lower()}",
            "download_url": payload.cad_url.strip(),
            "source_page": source_page,
            "repository": "PhysixCAD Community Uploads",
            "license": "Submitted by user; verify before commercial use",
            "source_type": "user_upload",
        },
        "physics": {
            "mass_grams": mass_grams,
            "material_composition": [
                {
                    "material": clean_upload_text(payload.material, "User specified material", 120),
                    "density_g_cm3": None,
                    "percentage": 100,
                }
            ],
            "center_of_mass_mm": {"x": 0, "y": 0, "z": 0},
            "motion": {
                "max_rotational_velocity_rpm": max_rpm,
                "holding_torque_nm": torque_nm,
            },
            "joint_constraints": [
                {
                    "type": clean_upload_text(payload.constraint_type, "fixed_body", 80),
                    "axis": "user_defined",
                    "limits_degrees": [0, 0],
                }
            ],
        },
        "media": {
            "image_url": "",
            "image_source": source_page,
            "alt": f"{name} community CAD preview",
        },
        "competition_relevance": payload.competition_relevance[:8],
        "metadata_quality": {
            "source": "community_upload",
            "confidence": "needs_review",
            "review_status": "pending_admin_review",
        },
        "created_at": utc_timestamp(),
    }


def engine_export_payload(part: dict[str, Any], engine: str) -> dict[str, Any]:
    engine_key = engine.lower().replace("_", "-")
    if engine_key not in {"unity", "unreal", "matlab", "ros-gazebo"}:
        raise HTTPException(status_code=404, detail="Unknown export preset.")

    physics = part["physics"]
    center_mm = physics.get("center_of_mass_mm", {})
    mass_kg = round(float(physics.get("mass_grams", 0)) / 1000.0, 6)
    base = {
        "part_id": part["id"],
        "part_name": part["name"],
        "category": part["category"],
        "cad": part["cad"],
        "material": primary_material(part),
        "mass_kg": mass_kg,
        "center_of_mass_m": {
            "x": round(float(center_mm.get("x", 0)) / 1000.0, 6),
            "y": round(float(center_mm.get("y", 0)) / 1000.0, 6),
            "z": round(float(center_mm.get("z", 0)) / 1000.0, 6),
        },
        "joint_constraints": physics.get("joint_constraints", []),
        "motion": physics.get("motion", {}),
        "generated_at": utc_timestamp(),
    }
    if engine_key == "unity":
        base["engine"] = "Unity"
        base["component_mapping"] = {
            "Rigidbody.mass": mass_kg,
            "Rigidbody.centerOfMass": base["center_of_mass_m"],
            "Collider.source": "Use imported CAD mesh collider or simplified convex collider",
        }
    elif engine_key == "unreal":
        base["engine"] = "Unreal Engine"
        base["component_mapping"] = {
            "BodyInstance.mass_kg": mass_kg,
            "CenterOfMassOffset_cm": {
                axis: round(value * 100.0, 4)
                for axis, value in base["center_of_mass_m"].items()
            },
            "PhysicalMaterial": primary_material(part),
        }
    elif engine_key == "matlab":
        base["engine"] = "MATLAB Simscape Multibody"
        base["component_mapping"] = {
            "Solid.Mass": {"value": mass_kg, "unit": "kg"},
            "Solid.CenterOfMass": {"value": base["center_of_mass_m"], "unit": "m"},
            "Geometry.Import": part["cad"]["filename"],
        }
    else:
        base["engine"] = "ROS / Gazebo"
        base["component_mapping"] = {
            "urdf.inertial.mass": mass_kg,
            "urdf.inertial.origin.xyz": base["center_of_mass_m"],
            "gazebo.material": primary_material(part),
        }
    return base


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
    parts = list_parts()
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


@app.get("/api/votes")
def api_votes(voter_id: str | None = Query(None, description="Anonymous browser id for returning this user's votes.")) -> dict[str, Any]:
    cleaned_voter_id = clean_client_id(voter_id) if voter_id else None
    with vote_lock:
        store = load_vote_store()
        part_records = store.get("parts", {})
        summaries = {
            part_id: summarize_vote_record(record)
            for part_id, record in part_records.items()
        }
        user_votes = {}
        if cleaned_voter_id:
            user_votes = {
                part_id: record.get("voters", {}).get(cleaned_voter_id)
                for part_id, record in part_records.items()
                if record.get("voters", {}).get(cleaned_voter_id)
            }
    return {"count": len(summaries), "votes": summaries, "user_votes": user_votes}


@app.get("/api/parts/{part_id}/votes")
def api_part_votes(part_id: str) -> dict[str, Any]:
    get_part(part_id)
    with vote_lock:
        store = load_vote_store()
        record = store.get("parts", {}).get(part_id)
        summary = summarize_vote_record(record)
    return {"part_id": part_id, "votes": summary}


@app.post("/api/parts/{part_id}/vote")
def api_part_vote(part_id: str, payload: VoteRequest) -> dict[str, Any]:
    get_part(part_id)
    voter_id = clean_client_id(payload.voter_id)
    if payload.vote not in {"genuine", "not_genuine"}:
        raise HTTPException(status_code=400, detail="Vote must be genuine or not_genuine.")

    with vote_lock:
        store = load_vote_store()
        part_records = store.setdefault("parts", {})
        record = part_records.setdefault(part_id, {"voters": {}, "created_at": utc_timestamp()})
        record.setdefault("voters", {})[voter_id] = payload.vote
        record["updated_at"] = utc_timestamp()
        save_vote_store(store)
        summary = summarize_vote_record(record)

    return {"part_id": part_id, "votes": summary, "user_vote": payload.vote}


@app.get("/api/presence")
def api_presence() -> dict[str, Any]:
    with presence_lock:
        online_count = prune_presence()
    return {"online_count": online_count, "timeout_seconds": PRESENCE_TIMEOUT_SECONDS}


@app.post("/api/presence/heartbeat")
def api_presence_heartbeat(payload: PresenceRequest) -> dict[str, Any]:
    client_id = clean_client_id(payload.client_id)
    with presence_lock:
        active_clients[client_id] = time.monotonic()
        online_count = prune_presence()
    return {"online_count": online_count, "timeout_seconds": PRESENCE_TIMEOUT_SECONDS}


@app.get("/api/collections")
def api_collections() -> dict[str, Any]:
    return {"count": len(COLLECTIONS), "collections": COLLECTIONS}


@app.post("/api/uploads")
def api_upload_model(payload: UploadRequest) -> dict[str, Any]:
    submitter_id = clean_client_id(payload.submitter_id)
    if not payload.cad_url.strip().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="CAD URL must be an http or https link.")

    part = build_uploaded_part(payload)
    part["submitter_id"] = submitter_id
    with upload_lock:
        store = load_json_store(UPLOADS_PATH, "uploads")
        store.setdefault("uploads", []).append(part)
        save_json_store(UPLOADS_PATH, store)
    return {"ok": True, "part": part}


@app.get("/api/uploads")
def api_uploads() -> dict[str, Any]:
    uploads = load_uploaded_parts()
    return {"count": len(uploads), "uploads": uploads}


@app.post("/api/parts/{part_id}/report")
def api_report_part(part_id: str, payload: ReportRequest) -> dict[str, Any]:
    part = get_part(part_id)
    reporter_id = clean_client_id(payload.reporter_id)
    reason = clean_upload_text(payload.reason, "Incorrect model metadata", 120)
    detail = clean_upload_text(payload.detail, "", 600)
    report = {
        "id": f"report-{safe_package_name(part_id)}-{int(time.time())}",
        "part_id": part_id,
        "part_name": part["name"],
        "reporter_id": reporter_id,
        "reason": reason,
        "detail": detail,
        "created_at": utc_timestamp(),
        "status": "open",
    }
    with report_lock:
        store = load_json_store(REPORTS_PATH, "reports")
        store.setdefault("reports", []).append(report)
        save_json_store(REPORTS_PATH, store)
    return {"ok": True, "report": report}


@app.get("/api/parts/{part_id}/export/{engine}")
def api_engine_export(part_id: str, engine: str) -> StreamingResponse:
    part = get_part(part_id)
    payload = engine_export_payload(part, engine)
    engine_slug = safe_package_name(payload["engine"])
    filename = f"{safe_package_name(part_id)}-{engine_slug}-physics.json"
    return StreamingResponse(
        BytesIO(json.dumps(payload, indent=2).encode("utf-8")),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/admin/stats")
def api_admin_stats() -> dict[str, Any]:
    parts = list_parts()
    with vote_lock:
        vote_store = load_vote_store()
    reports = load_json_store(REPORTS_PATH, "reports").get("reports", [])
    uploads = load_uploaded_parts()

    vote_summaries = {
        part_id: summarize_vote_record(record)
        for part_id, record in vote_store.get("parts", {}).items()
    }
    top_verified = sorted(
        (
            {"part_id": part_id, **summary}
            for part_id, summary in vote_summaries.items()
            if summary["total"] > 0
        ),
        key=lambda item: (item["score"], item["upvotes"], -item["downvotes"]),
        reverse=True,
    )[:10]
    flagged_by_votes = [
        {"part_id": part_id, **summary}
        for part_id, summary in vote_summaries.items()
        if summary["downvotes"] > summary["upvotes"] and summary["total"] > 0
    ][:20]
    open_reports = [report for report in reports if report.get("status") == "open"]
    return {
        "models": len(parts),
        "categories": len({part["category"] for part in parts}),
        "community_uploads": len(uploads),
        "total_votes": sum(summary["total"] for summary in vote_summaries.values()),
        "open_reports": len(open_reports),
        "flagged_by_votes": flagged_by_votes,
        "latest_reports": open_reports[-10:],
        "top_verified": top_verified,
        "collections": len(COLLECTIONS),
        "generated_at": utc_timestamp(),
    }


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
