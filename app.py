from __future__ import annotations

import json
import math
import re
import sqlite3
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
APP_DB_PATH = ROOT / "physixcad.sqlite3"
PRESENCE_TIMEOUT_SECONDS = 45.0

vote_lock = Lock()
report_lock = Lock()
upload_lock = Lock()
request_lock = Lock()
profile_lock = Lock()
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


class ProfileRequest(BaseModel):
    client_id: str
    display_name: str = ""
    email: str = ""
    role: str = "Student engineer"
    team: str = ""


class FavoriteRequest(BaseModel):
    client_id: str


class ModelRequestPayload(BaseModel):
    requester_id: str
    name: str
    category: str = "General Engineering"
    use_case: str = ""
    details: str = ""
    priority: str = "normal"


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


def db_connect() -> sqlite3.Connection:
    connection = sqlite3.connect(APP_DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_app_db() -> None:
    with db_connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS votes (
                part_id TEXT NOT NULL,
                voter_id TEXT NOT NULL,
                vote TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (part_id, voter_id)
            );

            CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY,
                part_id TEXT NOT NULL,
                part_name TEXT NOT NULL,
                reporter_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                detail TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS uploads (
                part_id TEXT PRIMARY KEY,
                submitter_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                client_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                email TEXT NOT NULL,
                role TEXT NOT NULL,
                team TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS favorites (
                client_id TEXT NOT NULL,
                part_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (client_id, part_id)
            );

            CREATE TABLE IF NOT EXISTS model_requests (
                id TEXT PRIMARY KEY,
                requester_id TEXT NOT NULL,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                use_case TEXT NOT NULL,
                details TEXT NOT NULL,
                priority TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        connection.commit()
    migrate_legacy_json_stores()


def migrate_legacy_json_stores() -> None:
    with db_connect() as connection:
        if VOTES_PATH.exists() and connection.execute("SELECT COUNT(*) FROM votes").fetchone()[0] == 0:
            try:
                vote_store = load_vote_store_json()
                for part_id, record in vote_store.get("parts", {}).items():
                    for voter_id, vote in record.get("voters", {}).items():
                        if vote in {"genuine", "not_genuine"}:
                            connection.execute(
                                """
                                INSERT OR IGNORE INTO votes
                                    (part_id, voter_id, vote, created_at, updated_at)
                                VALUES (?, ?, ?, ?, ?)
                                """,
                                (
                                    part_id,
                                    voter_id,
                                    vote,
                                    record.get("created_at") or utc_timestamp(),
                                    record.get("updated_at") or utc_timestamp(),
                                ),
                            )
            except (json.JSONDecodeError, OSError):
                pass

        if REPORTS_PATH.exists() and connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 0:
            try:
                for report in load_json_store(REPORTS_PATH, "reports").get("reports", []):
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO reports
                            (id, part_id, part_name, reporter_id, reason, detail, status, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            report.get("id") or f"report-{int(time.time())}",
                            report.get("part_id", ""),
                            report.get("part_name", ""),
                            report.get("reporter_id", ""),
                            report.get("reason", ""),
                            report.get("detail", ""),
                            report.get("status", "open"),
                            report.get("created_at") or utc_timestamp(),
                        ),
                    )
            except (json.JSONDecodeError, OSError):
                pass

        if UPLOADS_PATH.exists() and connection.execute("SELECT COUNT(*) FROM uploads").fetchone()[0] == 0:
            try:
                for part in load_json_store(UPLOADS_PATH, "uploads").get("uploads", []):
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO uploads
                            (part_id, submitter_id, payload_json, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            part.get("id"),
                            part.get("submitter_id", "legacy-json"),
                            json.dumps(part, sort_keys=True),
                            part.get("created_at") or utc_timestamp(),
                        ),
                    )
            except (json.JSONDecodeError, OSError):
                pass

        connection.commit()


def load_db_uploads() -> list[dict[str, Any]]:
    with db_connect() as connection:
        rows = connection.execute("SELECT payload_json FROM uploads ORDER BY created_at DESC").fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


def load_uploaded_parts() -> list[dict[str, Any]]:
    return load_db_uploads()


def list_parts() -> list[dict[str, Any]]:
    return [*load_database()["parts"], *load_uploaded_parts()]


def get_part(part_id: str) -> dict[str, Any]:
    for part in list_parts():
        if part["id"] == part_id:
            return enrich_part(part)
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


def load_vote_store_json() -> dict[str, Any]:
    if not VOTES_PATH.exists():
        return {"parts": {}, "created_at": utc_timestamp(), "updated_at": utc_timestamp()}
    with VOTES_PATH.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def load_vote_store() -> dict[str, Any]:
    with db_connect() as connection:
        rows = connection.execute(
            "SELECT part_id, voter_id, vote, created_at, updated_at FROM votes"
        ).fetchall()
    store: dict[str, Any] = {"parts": {}, "created_at": utc_timestamp(), "updated_at": utc_timestamp()}
    for row in rows:
        record = store["parts"].setdefault(
            row["part_id"],
            {
                "voters": {},
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            },
        )
        record["voters"][row["voter_id"]] = row["vote"]
        if row["updated_at"] > record.get("updated_at", ""):
            record["updated_at"] = row["updated_at"]
    return store


def save_vote_store(store: dict[str, Any]) -> None:
    now = utc_timestamp()
    with db_connect() as connection:
        connection.execute("DELETE FROM votes")
        for part_id, record in store.get("parts", {}).items():
            for voter_id, vote in record.get("voters", {}).items():
                if vote not in {"genuine", "not_genuine"}:
                    continue
                connection.execute(
                    """
                    INSERT OR REPLACE INTO votes
                        (part_id, voter_id, vote, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        part_id,
                        voter_id,
                        vote,
                        record.get("created_at") or now,
                        record.get("updated_at") or now,
                    ),
                )
        connection.commit()


init_app_db()


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


def license_profile(part: dict[str, Any]) -> dict[str, Any]:
    cad = part.get("cad", {})
    license_name = str(cad.get("license") or "License not specified").strip()
    license_key = license_name.lower()
    source_type = cad.get("source_type") or ("procedural" if "PhysixCAD" in str(cad.get("repository", "")) else "public_source")
    commercial_use = "verify"
    redistribution = "verify"
    attribution_required = "verify"
    notes = "Verify the source page before commercial redistribution."

    if "cc0" in license_key or "public domain" in license_key:
        commercial_use = "allowed"
        redistribution = "allowed"
        attribution_required = "not_required"
        notes = "CC0-style source; attribution is still recommended for engineering traceability."
    elif "cc-by" in license_key or "cc by" in license_key:
        commercial_use = "allowed_with_attribution"
        redistribution = "allowed_with_attribution"
        attribution_required = "required"
        notes = "Credit the original CAD/source repository when reusing or redistributing."
    elif source_type == "user_upload":
        commercial_use = "unknown"
        redistribution = "unknown"
        attribution_required = "unknown"
        notes = "Community upload; verify ownership and license before production use."

    return {
        "license": license_name,
        "source_type": source_type,
        "repository": cad.get("repository", "Unknown source"),
        "source_page": cad.get("source_page", ""),
        "download_url": cad.get("download_url", ""),
        "commercial_use": commercial_use,
        "redistribution": redistribution,
        "attribution_required": attribution_required,
        "notes": notes,
    }


def simulation_readiness(part: dict[str, Any]) -> dict[str, Any]:
    cad = part.get("cad", {})
    physics = part.get("physics", {})
    media = part.get("media", {})
    quality = part.get("metadata_quality", {})
    checks: list[dict[str, Any]] = []

    def add_check(key: str, label: str, passed: bool, points: int) -> int:
        checks.append({"key": key, "label": label, "passed": passed, "points": points if passed else 0})
        return points if passed else 0

    score = 0
    score += add_check("cad_file", "CAD download or generated model available", bool(cad.get("download_url") or cad.get("source_type") == "procedural"), 16)
    score += add_check("cad_format", "CAD format declared", bool(cad.get("format") and cad.get("filename")), 8)
    score += add_check("source_license", "Source and license documented", bool(cad.get("source_page") and cad.get("license")), 14)
    score += add_check("mass", "Mass metadata present", float(physics.get("mass_grams") or 0) > 0, 12)
    score += add_check("material", "Material composition present", bool(physics.get("material_composition")), 12)
    center = physics.get("center_of_mass_mm") or {}
    score += add_check("center_of_mass", "Center of mass coordinates present", all(axis in center for axis in ("x", "y", "z")), 12)
    score += add_check("constraints", "Joint or assembly constraint defined", bool(physics.get("joint_constraints")), 10)
    score += add_check("motion", "Motion/torque/RPM metadata present", bool(physics.get("motion")), 8)
    score += add_check("image", "Visual part image available", bool(media.get("image_url")), 4)
    score += add_check("review", "No vendor review warning", not bool(quality.get("requires_vendor_review")), 4)

    missing = [check["label"] for check in checks if not check["passed"]]
    if score >= 90:
        grade = "A"
        status = "Simulation ready"
    elif score >= 78:
        grade = "B"
        status = "Ready for prototyping"
    elif score >= 62:
        grade = "C"
        status = "Needs engineering review"
    else:
        grade = "D"
        status = "Metadata incomplete"
    return {"score": score, "grade": grade, "status": status, "checks": checks, "missing": missing}


def physix_verified(part: dict[str, Any]) -> dict[str, Any]:
    profile = license_profile(part)
    readiness = simulation_readiness(part)
    quality = part.get("metadata_quality", {})
    requires_review = bool(quality.get("requires_vendor_review"))
    source_ok = profile["source_type"] in {"public_source", "procedural"}
    verified = source_ok and not requires_review and readiness["score"] >= 82
    return {
        "verified": verified,
        "label": "PhysixCAD Verified" if verified else "Engineering Review Needed",
        "reason": "Source, license, CAD, and physics metadata are complete enough for simulation prototyping."
        if verified
        else "Use this model as a starting point and verify dimensions/metadata before critical engineering work.",
    }


def enrich_part(part: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(part)
    enriched["license_profile"] = license_profile(part)
    enriched["simulation_readiness"] = simulation_readiness(part)
    enriched["physix_verification"] = physix_verified(part)
    return enriched


def enriched_parts() -> list[dict[str, Any]]:
    return [enrich_part(part) for part in list_parts()]


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


def upsert_profile(payload: ProfileRequest) -> dict[str, Any]:
    client_id = clean_client_id(payload.client_id)
    now = utc_timestamp()
    display_name = clean_upload_text(payload.display_name, "PhysixCAD Engineer", 80)
    email = clean_upload_text(payload.email, "", 120)
    role = clean_upload_text(payload.role, "Student engineer", 80)
    team = clean_upload_text(payload.team, "", 120)
    with db_connect() as connection:
        connection.execute(
            """
            INSERT INTO users (client_id, display_name, email, role, team, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_id) DO UPDATE SET
                display_name = excluded.display_name,
                email = excluded.email,
                role = excluded.role,
                team = excluded.team,
                updated_at = excluded.updated_at
            """,
            (client_id, display_name, email, role, team, now, now),
        )
        connection.commit()
    return get_profile(client_id)


def get_profile(client_id: str) -> dict[str, Any]:
    client_id = clean_client_id(client_id)
    with db_connect() as connection:
        row = connection.execute(
            "SELECT client_id, display_name, email, role, team, created_at, updated_at FROM users WHERE client_id = ?",
            (client_id,),
        ).fetchone()
    if not row:
        return {
            "client_id": client_id,
            "display_name": "",
            "email": "",
            "role": "",
            "team": "",
            "created_at": None,
            "updated_at": None,
        }
    return dict(row)


def favorite_ids(client_id: str) -> list[str]:
    client_id = clean_client_id(client_id)
    with db_connect() as connection:
        rows = connection.execute(
            "SELECT part_id FROM favorites WHERE client_id = ? ORDER BY created_at DESC",
            (client_id,),
        ).fetchall()
    return [row["part_id"] for row in rows]


def favorite_parts(client_id: str) -> list[dict[str, Any]]:
    ids = set(favorite_ids(client_id))
    return [part for part in enriched_parts() if part["id"] in ids]


def toggle_favorite(client_id: str, part_id: str) -> dict[str, Any]:
    client_id = clean_client_id(client_id)
    get_part(part_id)
    with db_connect() as connection:
        exists = connection.execute(
            "SELECT 1 FROM favorites WHERE client_id = ? AND part_id = ?",
            (client_id, part_id),
        ).fetchone()
        if exists:
            connection.execute(
                "DELETE FROM favorites WHERE client_id = ? AND part_id = ?",
                (client_id, part_id),
            )
            favorited = False
        else:
            connection.execute(
                "INSERT INTO favorites (client_id, part_id, created_at) VALUES (?, ?, ?)",
                (client_id, part_id, utc_timestamp()),
            )
            favorited = True
        connection.commit()
    ids = favorite_ids(client_id)
    return {"part_id": part_id, "favorited": favorited, "favorite_ids": ids, "count": len(ids)}


def create_model_request(payload: ModelRequestPayload) -> dict[str, Any]:
    requester_id = clean_client_id(payload.requester_id)
    request = {
        "id": f"request-{safe_package_name(payload.name)}-{int(time.time())}",
        "requester_id": requester_id,
        "name": clean_upload_text(payload.name, "Requested CAD model", 120),
        "category": clean_upload_text(payload.category, "General Engineering", 80),
        "use_case": clean_upload_text(payload.use_case, "", 220),
        "details": clean_upload_text(payload.details, "", 800),
        "priority": clean_upload_text(payload.priority, "normal", 40),
        "status": "open",
        "created_at": utc_timestamp(),
    }
    with db_connect() as connection:
        connection.execute(
            """
            INSERT INTO model_requests
                (id, requester_id, name, category, use_case, details, priority, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request["id"],
                request["requester_id"],
                request["name"],
                request["category"],
                request["use_case"],
                request["details"],
                request["priority"],
                request["status"],
                request["created_at"],
            ),
        )
        connection.commit()
    return request


def list_model_requests(limit: int = 30) -> list[dict[str, Any]]:
    limit = max(1, min(100, int(limit)))
    with db_connect() as connection:
        rows = connection.execute(
            """
            SELECT id, requester_id, name, category, use_case, details, priority, status, created_at
            FROM model_requests
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


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
                    "simulation_readiness": part.get("simulation_readiness") or simulation_readiness(part),
                    "license_profile": part.get("license_profile") or license_profile(part),
                    "physix_verification": part.get("physix_verification") or physix_verified(part),
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
    parts = enriched_parts()
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


@app.get("/api/parts/{part_id}/preview-stl")
def api_part_preview_stl(part_id: str) -> StreamingResponse:
    part = get_part(part_id)
    cad = part.get("cad", {})
    stl_bytes: bytes
    status = "generated-fallback"
    try:
        if cad.get("source_type") == "procedural":
            stl_bytes = procedural_stl(part)
            status = "generated-procedural"
        elif str(cad.get("filename", "")).lower().endswith(".stl") or str(cad.get("download_url", "")).lower().split("?", 1)[0].endswith(".stl"):
            stl_bytes, _ = fetch_remote_asset(cad["download_url"], timeout_seconds=8.0)
            status = "source-stl"
        else:
            stl_bytes = box_mesh_stl(part["name"], fallback_dimensions(part)).encode("utf-8")
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        stl_bytes = box_mesh_stl(part["name"], fallback_dimensions(part)).encode("utf-8")
    return StreamingResponse(
        BytesIO(stl_bytes),
        media_type="model/stl",
        headers={
            "Content-Disposition": f'inline; filename="{safe_package_name(part_id)}-preview.stl"',
            "X-PhysixCAD-Preview": status,
        },
    )


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


@app.get("/api/compare")
def api_compare(ids: str = Query(..., description="Comma-separated part ids to compare, up to four.")) -> dict[str, Any]:
    requested_ids = [part_id.strip() for part_id in ids.split(",") if part_id.strip()]
    if not 2 <= len(requested_ids) <= 4:
        raise HTTPException(status_code=400, detail="Compare requires 2 to 4 part ids.")
    parts = [get_part(part_id) for part_id in requested_ids]
    metrics = [
        {"key": "mass_grams", "label": "Mass (g)", "values": {part["id"]: part["physics"].get("mass_grams") for part in parts}},
        {"key": "material", "label": "Material", "values": {part["id"]: primary_material(part) for part in parts}},
        {"key": "motion", "label": "Motion", "values": {part["id"]: part["physics"].get("motion", {}) for part in parts}},
        {"key": "constraint", "label": "Constraint", "values": {part["id"]: part["physics"].get("joint_constraints", [{}])[0] for part in parts}},
        {"key": "readiness", "label": "Readiness", "values": {part["id"]: part["simulation_readiness"]["score"] for part in parts}},
        {"key": "license", "label": "License", "values": {part["id"]: part["license_profile"]["license"] for part in parts}},
    ]
    return {"count": len(parts), "parts": parts, "metrics": metrics, "generated_at": utc_timestamp()}


@app.get("/api/profile")
def api_profile(client_id: str = Query(...)) -> dict[str, Any]:
    profile = get_profile(client_id)
    favorites = favorite_ids(client_id)
    return {"profile": profile, "favorite_ids": favorites, "favorite_count": len(favorites)}


@app.post("/api/profile")
def api_save_profile(payload: ProfileRequest) -> dict[str, Any]:
    with profile_lock:
        profile = upsert_profile(payload)
    favorites = favorite_ids(payload.client_id)
    return {"ok": True, "profile": profile, "favorite_ids": favorites, "favorite_count": len(favorites)}


@app.get("/api/favorites")
def api_favorites(client_id: str = Query(...)) -> dict[str, Any]:
    favorites = favorite_parts(client_id)
    return {"count": len(favorites), "favorite_ids": [part["id"] for part in favorites], "parts": favorites}


@app.post("/api/parts/{part_id}/favorite")
def api_toggle_favorite(part_id: str, payload: FavoriteRequest) -> dict[str, Any]:
    with profile_lock:
        return toggle_favorite(payload.client_id, part_id)


@app.post("/api/model-requests")
def api_create_model_request(payload: ModelRequestPayload) -> dict[str, Any]:
    with request_lock:
        request = create_model_request(payload)
    return {"ok": True, "request": request}


@app.get("/api/model-requests")
def api_model_requests(limit: int = Query(30, ge=1, le=100)) -> dict[str, Any]:
    requests = list_model_requests(limit)
    return {"count": len(requests), "requests": requests}


@app.post("/api/uploads")
def api_upload_model(payload: UploadRequest) -> dict[str, Any]:
    submitter_id = clean_client_id(payload.submitter_id)
    if not payload.cad_url.strip().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="CAD URL must be an http or https link.")

    part = build_uploaded_part(payload)
    part["submitter_id"] = submitter_id
    with upload_lock:
        with db_connect() as connection:
            connection.execute(
                """
                INSERT INTO uploads (part_id, submitter_id, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (part["id"], submitter_id, json.dumps(part, sort_keys=True), part["created_at"]),
            )
            connection.commit()
    return {"ok": True, "part": enrich_part(part)}


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
        with db_connect() as connection:
            connection.execute(
                """
                INSERT INTO reports
                    (id, part_id, part_name, reporter_id, reason, detail, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report["id"],
                    report["part_id"],
                    report["part_name"],
                    report["reporter_id"],
                    report["reason"],
                    report["detail"],
                    report["status"],
                    report["created_at"],
                ),
            )
            connection.commit()
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
    parts = enriched_parts()
    with vote_lock:
        vote_store = load_vote_store()
    with db_connect() as connection:
        reports = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, part_id, part_name, reporter_id, reason, detail, status, created_at
                FROM reports
                ORDER BY created_at DESC
                """
            ).fetchall()
        ]
        user_count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        favorite_count = connection.execute("SELECT COUNT(*) FROM favorites").fetchone()[0]
        request_count = connection.execute("SELECT COUNT(*) FROM model_requests WHERE status = 'open'").fetchone()[0]
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
    readiness_scores = [part.get("simulation_readiness", {}).get("score", 0) for part in parts]
    verified_count = sum(1 for part in parts if part.get("physix_verification", {}).get("verified"))
    return {
        "models": len(parts),
        "categories": len({part["category"] for part in parts}),
        "community_uploads": len(uploads),
        "total_votes": sum(summary["total"] for summary in vote_summaries.values()),
        "open_reports": len(open_reports),
        "model_requests": request_count,
        "profiles": user_count,
        "favorites": favorite_count,
        "physix_verified": verified_count,
        "average_readiness": round(sum(readiness_scores) / len(readiness_scores), 1) if readiness_scores else 0,
        "flagged_by_votes": flagged_by_votes,
        "latest_reports": open_reports[:10],
        "latest_requests": list_model_requests(10),
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
