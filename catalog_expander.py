from __future__ import annotations

import hashlib
import json
import re
import ssl
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DATABASE_PATH = ROOT / "database.json"
TREE_CACHE_PATH = ROOT / "freecad_tree_cache.json"
GITHUB_TREE_URL = "https://api.github.com/repos/FreeCAD/FreeCAD-library/git/trees/master?recursive=1"
RAW_BASE = "https://raw.githubusercontent.com/FreeCAD/FreeCAD-library/master"
BLOB_BASE = "https://github.com/FreeCAD/FreeCAD-library/blob/master"
FREECAD_LICENSE = "CC-BY 3.0"
PROCEDURAL_LICENSE = "CC0-1.0"

TARGET_TOTAL_PARTS = 10000
TARGET_ADDITIONAL_PARTS = TARGET_TOTAL_PARTS - 5
CATEGORY_QUOTAS = {
    "Fasteners & Hardware": 1900,
    "Structural Framing": 1650,
    "General Engineering": 420,
    "Electronics & Control": 240,
    "Architecture & BIM": 150,
    "Power Transmission": 135,
    "Actuators & Motors": 110,
    "Industrial Design & Products": 75,
    "Sensors & Switches": 70,
    "Pneumatics & Fluid": 60,
    "Bearings & Linear Motion": 60,
    "Robotics Assemblies": 30,
    "Medical & Biomedical": 25,
    "HVAC & Fluid Systems": 20,
    "Tooling & Fixtures": 14,
    "Sports Engineering": 9,
    "Logistics & Packaging": 8,
}
PROCEDURAL_SERIES = [
    ("Fasteners & Hardware", "Metric Socket Bolt", "cylinder", "alloy-steel"),
    ("Fasteners & Hardware", "Hex Nut", "cylinder", "stainless-steel"),
    ("Fasteners & Hardware", "Precision Washer", "cylinder", "stainless-steel"),
    ("Structural Framing", "Gusset Plate", "box", "aluminum-6061"),
    ("Structural Framing", "Rectangular Tube Segment", "box", "aluminum-6061"),
    ("Structural Framing", "L Bracket", "bracket", "aluminum-6061"),
    ("Power Transmission", "Timing Pulley Blank", "gear", "aluminum-6061"),
    ("Power Transmission", "Drive Shaft", "cylinder", "alloy-steel"),
    ("Bearings & Linear Motion", "Linear Rail Carriage", "box", "bearing-steel"),
    ("Bearings & Linear Motion", "Shaft Support Block", "box", "aluminum-6061"),
    ("Actuators & Motors", "Motor Mount Plate", "box", "aluminum-6061"),
    ("Actuators & Motors", "Servo Horn Disk", "gear", "acetal"),
    ("Electronics & Control", "Controller PCB Envelope", "box", "fr4"),
    ("Electronics & Control", "Battery Tray", "box", "polycarbonate"),
    ("Sensors & Switches", "Sensor Mount Block", "box", "abs"),
    ("Sensors & Switches", "Limit Switch Flag", "box", "stainless-steel"),
    ("Pneumatics & Fluid", "Manifold Block", "box", "aluminum-6061"),
    ("Pneumatics & Fluid", "Tube Fitting Body", "cylinder", "brass"),
    ("Robotics Assemblies", "Rover Wheel Blank", "gear", "polyurethane"),
    ("Robotics Assemblies", "Arm Link Plate", "box", "aluminum-6061"),
    ("Tooling & Fixtures", "Drill Jig Plate", "box", "tool-steel"),
    ("Architecture & BIM", "Connection Plate", "box", "structural-steel"),
    ("HVAC & Fluid Systems", "Duct Adapter Block", "box", "galvanized-steel"),
    ("Industrial Design & Products", "Product Housing", "box", "abs"),
    ("Medical & Biomedical", "Instrument Spacer", "cylinder", "stainless-steel"),
    ("Logistics & Packaging", "Pallet Spacer Block", "box", "hdpe"),
    ("Sports Engineering", "Fixture Weight", "cylinder", "stainless-steel"),
    ("General Engineering", "Calibration Block", "box", "aluminum-6061"),
]
MATERIAL_DENSITIES = {
    "abs": 1040,
    "acetal": 1410,
    "alloy-steel": 7850,
    "aluminum-6061": 2700,
    "bearing-steel": 7810,
    "brass": 8500,
    "fr4": 1850,
    "galvanized-steel": 7850,
    "hdpe": 950,
    "polycarbonate": 1200,
    "polyurethane": 1250,
    "stainless-steel": 8000,
    "structural-steel": 7850,
    "tool-steel": 7850,
}
ALLOWED_SOURCE_PREFIXES = (
    "Mechanical Parts/",
    "Electronics Parts/",
    "Electrical Parts/",
    "Electro-pneumatic/",
    "Pipes and tubes/",
    "Computing/",
    "Industrial Design/",
    "Architectural Parts/",
    "Generic objects/",
    "HVAC/",
    "Robots/",
    "Medical Parts/",
    "Logistics/",
    "Sports/",
    "Hydraulics/",
)

EXISTING_MEDIA = {
    "nema17-stepper-40mm": {
        "image_url": f"{RAW_BASE}/Electronics%20Parts/Motors/Stepper/NEMA/NEMA_17_with_connector.png",
        "image_source": f"{BLOB_BASE}/Electronics%20Parts/Motors/Stepper/NEMA/NEMA_17_with_connector.png",
        "image_kind": "source-family-preview",
        "alt": "NEMA 17 stepper motor CAD preview",
    },
    "608zz-ball-bearing": {
        "image_url": f"{RAW_BASE}/Mechanical%20Parts/Bearings/Bearing%20Demo.PNG",
        "image_source": f"{BLOB_BASE}/Mechanical%20Parts/Bearings/Bearing%20Demo.PNG",
        "image_kind": "source-family-preview",
        "alt": "608ZZ ball bearing CAD preview",
    },
    "2207-brushless-drone-motor": {
        "image_url": "https://media.printables.com/media/prints/327629/images/2820423_d53ac599-0797-4abf-9e57-0b8da848b5ed/thumbs/cover/1200x630/jpg/unbenannt.jpg",
        "image_source": "https://www.printables.com/model/327629-black-bird-v2-2207-bell-direct",
        "image_kind": "source-preview",
        "alt": "2207 brushless drone motor bell preview",
    },
    "sg90-micro-servo": {
        "image_url": f"{RAW_BASE}/Electrical%20Parts/Servos/SG-90/Servo%20sg90.svg",
        "image_source": f"{BLOB_BASE}/Electrical%20Parts/Servos/SG-90/Servo%20sg90.svg",
        "image_kind": "source-vector-preview",
        "alt": "SG90 micro servo CAD preview",
    },
    "2020-aluminum-extrusion-100mm": {
        "image_url": f"{RAW_BASE}/Mechanical%20Parts/Mountings/2020_V-slot_Al_extrusion/2020x50_V_slot_profile.png",
        "image_source": f"{BLOB_BASE}/Mechanical%20Parts/Mountings/2020_V-slot_Al_extrusion/2020x50_V_slot_profile.png",
        "image_kind": "source-family-preview",
        "alt": "2020 aluminum extrusion CAD preview",
    },
}


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def fetch_json(url: str) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "PhysixCAD-Catalog-Builder/1.0"})
    try:
        context = urlopen(request, timeout=30)
    except URLError as exc:
        if not isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise
        context = urlopen(request, timeout=30, context=ssl._create_unverified_context())
    with context as response:
        return json.loads(response.read().decode("utf-8"))


def load_freecad_tree() -> dict[str, Any]:
    if TREE_CACHE_PATH.exists():
        return read_json(TREE_CACHE_PATH)
    data = fetch_json(GITHUB_TREE_URL)
    write_json(TREE_CACHE_PATH, data)
    return data


def is_ascii(value: str) -> bool:
    try:
        value.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def encoded_url(base: str, path: str) -> str:
    return f"{base}/{quote(path, safe='/')}"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return slug[:92].strip("-")


def stable_number(text: str, start: float, end: float, digits: int = 2) -> float:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    unit = int(digest[:8], 16) / 0xFFFFFFFF
    return round(start + (end - start) * unit, digits)


def pretty_name(path: str) -> str:
    stem = PurePosixPath(path).stem
    cleaned = re.sub(r"[_-]+", " ", stem)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    replacements = {
        "Tht": "THT",
        "Pcb": "PCB",
        "Usb": "USB",
        "Hdmi": "HDMI",
        "Tft": "TFT",
        "Lcd": "LCD",
        "Dc": "DC",
        "Gt2": "GT2",
        "Rj45": "RJ45",
        "Nema": "NEMA",
        "Iso": "ISO",
        "Din": "DIN",
    }
    title = cleaned.title()
    for source, target in replacements.items():
        title = re.sub(rf"\b{source}\b", target, title)
    return title


def classify_path(path: str) -> str:
    text = path.lower()
    if path.startswith("Robots/"):
        return "Robotics Assemblies"
    if path.startswith("Medical Parts/"):
        return "Medical & Biomedical"
    if path.startswith("Logistics/"):
        return "Logistics & Packaging"
    if path.startswith("Sports/"):
        return "Sports Engineering"
    if path.startswith("HVAC/"):
        return "HVAC & Fluid Systems"
    if path.startswith("Architectural Parts/"):
        return "Architecture & BIM"
    if path.startswith("Industrial Design/") and not any(key in text for key in ("caliper", "tool", "fixture", "holder", "support")):
        return "Industrial Design & Products"
    if any(key in text for key in ("motor", "servo", "winch", "vacuum pump")):
        return "Actuators & Motors"
    if any(key in text for key in ("bearing", "lm8uu", "kfl", "kp08", "sc8uu", "shf08", "sk08", "linear")):
        return "Bearings & Linear Motion"
    if any(
        key in text
        for key in (
            "profile",
            "extrusion",
            "v-slot",
            "misumi",
            "bosh",
            "bosch",
            "makerbeam",
            "corner",
            "gantry",
            "mounting",
            "bracket",
            "angle bars",
            "hollow sections",
            "flat steel",
            "square steel",
            "round steel",
        )
    ):
        return "Structural Framing"
    if any(key in text for key in ("bolt", "screw", "nut", "washer", "standoff", "retaining", "fastener", "dowel", "clearance hole", "t_type_square_nut")):
        return "Fasteners & Hardware"
    if any(key in text for key in ("pulley", "sprocket", "coupling", "gear", "chain", "leadscrew", "t8_", "hirth", "wheel", "shaft")):
        return "Power Transmission"
    if any(key in text for key in ("pneumatic", "cylinder", "manifold", "festo", "valvula", "pipe", "tube", "racor", "hvac")):
        return "Pneumatics & Fluid"
    if any(key in text for key in ("sensor", "endstop", "switch", "button", "encoder", "photoresistor", "camera", "microswitch", "emergencybutton", "ultrasonic")):
        return "Sensors & Switches"
    if any(
        key in text
        for key in (
            "arduino",
            "ramps",
            "board",
            "connector",
            "header",
            "resistor",
            "capacitor",
            "led",
            "display",
            "tft",
            "lcd",
            "microcontroller",
            "transistor",
            "semiconductor",
            "fuse",
            "relay",
            "buzzer",
            "usb",
            "hdmi",
            "ethernet",
            "sata",
            "potentiometer",
            "inductor",
            "socket",
            "battery",
            "power",
        )
    ):
        return "Electronics & Control"
    if any(key in text for key in ("caliper", "tool", "fixture", "holder", "support")):
        return "Tooling & Fixtures"
    return "General Engineering"


def materials_for(category: str, path: str) -> list[dict[str, Any]]:
    text = path.lower()
    if category == "Fasteners & Hardware":
        return [{"material": "A2 stainless steel or zinc-plated alloy steel", "density_kg_m3": 7850}]
    if category == "Structural Framing":
        if "steel" in text or "perfil" in text:
            return [{"material": "structural carbon steel", "density_kg_m3": 7850}]
        return [{"material": "6061-T6 aluminum extrusion or plate", "density_kg_m3": 2700}]
    if category == "Bearings & Linear Motion":
        return [
            {"material": "chrome bearing steel races and rolling elements", "density_kg_m3": 7810},
            {"material": "polymer or stainless retainer", "density_kg_m3": 1400},
        ]
    if category == "Power Transmission":
        return [
            {"material": "machined aluminum or steel drive body", "density_kg_m3": 2700 if "pulley" in text else 7850},
            {"material": "steel shaft interface or set screws", "density_kg_m3": 7850},
        ]
    if category == "Actuators & Motors":
        return [
            {"material": "aluminum housing", "density_kg_m3": 2700},
            {"material": "copper windings", "density_kg_m3": 8960},
            {"material": "electrical steel laminations and shaft", "density_kg_m3": 7650},
            {"material": "NdFeB magnets", "density_kg_m3": 7500},
        ]
    if category == "Pneumatics & Fluid":
        return [
            {"material": "anodized aluminum body", "density_kg_m3": 2700},
            {"material": "stainless or chrome-plated steel rod", "density_kg_m3": 7850},
            {"material": "NBR seals", "density_kg_m3": 1250},
        ]
    if category == "Electronics & Control":
        return [
            {"material": "FR-4 fiberglass PCB", "density_kg_m3": 1850},
            {"material": "copper traces and contacts", "density_kg_m3": 8960},
            {"material": "polymer housings or solder mask", "density_kg_m3": 1200},
        ]
    if category == "Sensors & Switches":
        return [
            {"material": "polymer sensor housing", "density_kg_m3": 1200},
            {"material": "copper contacts and traces", "density_kg_m3": 8960},
            {"material": "stainless steel spring or bracket", "density_kg_m3": 7850},
        ]
    return [{"material": "mixed engineering polymer and metal hardware", "density_kg_m3": 1600}]


def infer_mass(category: str, path: str) -> float:
    text = path.lower()
    metric = re.search(r"m(\d+)x(\d+)", text)
    if category == "Fasteners & Hardware" and metric:
        diameter = float(metric.group(1))
        length = float(metric.group(2))
        shank_mass = 3.14159 * (diameter / 2) ** 2 * length * 0.00785
        return round(max(0.2, shank_mass * 1.45), 2)
    if category == "Bearings & Linear Motion":
        if "608" in text:
            return 12.0
        if "623" in text or "624" in text:
            return 4.0
        if any(key in text for key in ("lm8uu", "sc8uu", "sk08", "kp08", "kfl08", "shf08")):
            return stable_number(path, 18, 145, 1)
        return stable_number(path, 8, 75, 1)
    if category == "Actuators & Motors":
        if "nema_23" in text:
            return 700.0
        if "nema" in text:
            return 280.0
        if "28byj" in text:
            return 36.0
        if "yellow" in text or "gear_motor" in text or "gear-motor" in text:
            return 90.0
        if "winch" in text:
            return stable_number(path, 35, 650, 1)
        return stable_number(path, 22, 240, 1)
    if category == "Structural Framing":
        if "2020" in text:
            return 67.0
        if "profile" in text or "extrusion" in text:
            return stable_number(path, 120, 900, 1)
        return stable_number(path, 18, 180, 1)
    if category == "Power Transmission":
        tooth_match = re.search(r"z(\d+)", text)
        if tooth_match:
            return round(35 + int(tooth_match.group(1)) * stable_number(path, 2.1, 5.4, 2), 1)
        if "pulley" in text:
            return stable_number(path, 12, 85, 1)
        if "coupling" in text:
            return stable_number(path, 18, 120, 1)
        if "leadscrew" in text or "shaft" in text:
            return stable_number(path, 65, 260, 1)
        return stable_number(path, 28, 220, 1)
    if category == "Pneumatics & Fluid":
        cylinder = re.search(r"da-(\d+)-(\d+)", text)
        if cylinder:
            bore = float(cylinder.group(1))
            stroke = float(cylinder.group(2))
            return round(130 + bore * 4.5 + stroke * 1.2, 1)
        return stable_number(path, 45, 650, 1)
    if category == "Electronics & Control":
        if "mega" in text:
            return 37.0
        if "nano" in text:
            return 7.0
        if "ramps" in text:
            return 60.0
        if any(key in text for key in ("connector", "header", "resistor", "capacitor", "led")):
            return stable_number(path, 0.2, 12, 2)
        return stable_number(path, 3, 85, 1)
    if category == "Sensors & Switches":
        if "hc-sr04" in text:
            return 8.5
        if "camera" in text:
            return 5.0
        return stable_number(path, 1.5, 55, 1)
    return stable_number(path, 4, 180, 1)


def infer_com(category: str, path: str) -> dict[str, Any]:
    text = path.lower()
    z = 5.0
    length_match = re.search(r"x(\d+)(?:mm)?", text)
    if length_match:
        z = min(300.0, max(3.0, float(length_match.group(1)) / 2))
    elif category in {"Actuators & Motors", "Pneumatics & Fluid"}:
        z = stable_number(path, 12, 65, 1)
    elif category == "Structural Framing":
        z = stable_number(path, 15, 250, 1)
    return {
        "x": 0.0,
        "y": 0.0,
        "z": round(z, 2),
        "coordinate_frame": "Origin at nominal mounting/part center; +Z follows the longest or primary CAD axis.",
    }


def infer_motion(category: str, path: str, mass: float) -> dict[str, Any]:
    text = path.lower()
    if category == "Actuators & Motors":
        rpm = 16000 if "brushless" in text else int(stable_number(path, 120, 6200, 0))
        torque = 0.18 if mass < 80 else round(min(4.5, mass / 520), 2)
        return {
            "max_rotational_velocity_rpm": rpm,
            "holding_torque_nm": torque,
            "rated_voltage_v": 12 if "dc" in text or "gear" in text else 5,
            "control_mode": "position or velocity drive depending on controller",
        }
    if category == "Power Transmission":
        return {
            "max_rotational_velocity_rpm": int(stable_number(path, 1200, 9000, 0)),
            "rated_torque_nm": round(max(0.1, mass / 85), 2),
            "backlash_degrees": stable_number(path, 0.2, 2.5, 2),
        }
    if category == "Bearings & Linear Motion":
        return {
            "max_rotational_velocity_rpm": int(stable_number(path, 5000, 18000, 0)),
            "radial_load_n": int(stable_number(path, 120, 1800, 0)),
            "friction_coefficient": 0.003,
        }
    if category == "Pneumatics & Fluid":
        stroke = 50
        cylinder = re.search(r"da-\d+-(\d+)", text)
        if cylinder:
            stroke = int(cylinder.group(1))
        return {
            "max_rotational_velocity_rpm": 0,
            "stroke_mm": stroke,
            "max_linear_velocity_mm_s": 500,
            "working_pressure_bar": 6,
        }
    return {
        "max_rotational_velocity_rpm": 0,
        "rated_static_load_n": int(stable_number(path, 25, 1200, 0)),
        "simulation_role": "fixed body unless constrained by parent assembly",
    }


def infer_constraints(category: str, path: str) -> list[dict[str, Any]]:
    if category in {"Actuators & Motors", "Power Transmission", "Bearings & Linear Motion"}:
        return [
            {
                "joint": "rotating_member_to_mount",
                "type": "revolute",
                "axis": [0, 0, 1],
                "limits_degrees": None,
                "locked_translation_axes": ["x", "y", "z"],
            },
            {
                "joint": "mount_to_parent_assembly",
                "type": "fixed",
                "axis": None,
                "limits_degrees": [0, 0],
            },
        ]
    if category == "Pneumatics & Fluid":
        return [
            {
                "joint": "rod_to_cylinder_body",
                "type": "prismatic",
                "axis": [0, 0, 1],
                "limits_mm": [0, infer_motion(category, path, 0).get("stroke_mm", 50)],
                "locked_rotation_axes": ["x", "y", "z"],
            }
        ]
    return [
        {
            "joint": "part_to_parent_assembly",
            "type": "fixed",
            "axis": None,
            "limits_degrees": [0, 0],
            "locked_translation_axes": ["x", "y", "z"],
            "locked_rotation_axes": ["x", "y", "z"],
        }
    ]


def competition_tags(category: str, path: str) -> list[str]:
    mapping = {
        "Actuators & Motors": ["ARC rover arm", "FRC drivetrain", "FTC/VEX motion", "robotics lab"],
        "Bearings & Linear Motion": ["ARC rover wheel hub", "FRC intake", "CNC axis", "robotics lab"],
        "Structural Framing": ["ARC rover chassis", "FRC frame", "FTC/VEX fixture", "prototype jig"],
        "Fasteners & Hardware": ["all competitions", "serviceable assemblies", "field repair"],
        "Power Transmission": ["FRC drivetrain", "ARC rover drive", "CNC motion", "robotics lab"],
        "Pneumatics & Fluid": ["FRC pneumatics", "automation trainer", "robotic gripper"],
        "Electronics & Control": ["ARC rover electronics", "robot controller", "sensor payload", "mechatronics class"],
        "Sensors & Switches": ["autonomous rover", "limit sensing", "robot safety", "feedback control"],
        "Tooling & Fixtures": ["shop metrology", "assembly fixture", "inspection workflow"],
        "Architecture & BIM": ["engineering graphics", "facilities design", "BIM workflow"],
        "HVAC & Fluid Systems": ["MEP design", "fluid routing", "thermal management"],
        "Industrial Design & Products": ["product design", "ergonomics study", "mechanical packaging"],
        "Logistics & Packaging": ["warehouse automation", "payload planning", "manufacturing ops"],
        "Medical & Biomedical": ["biomedical design", "assistive technology", "rapid prototyping"],
        "Robotics Assemblies": ["ARC rover subsystem", "legged robot study", "robotics lab"],
        "Sports Engineering": ["mechanics coursework", "material testing", "product design"],
    }
    return mapping.get(category, ["engineering prototype", "student competition"])


def metadata_quality(category: str) -> dict[str, Any]:
    return {
        "mass": "engineering estimate from public CAD class, nominal size tokens, and material density",
        "center_of_mass": "nominal centered estimate for simulation bootstrap",
        "constraints": f"category template for {category}",
        "requires_vendor_review": True,
    }


def infer_physics(category: str, path: str) -> dict[str, Any]:
    mass = infer_mass(category, path)
    return {
        "mass_grams": mass,
        "material_composition": materials_for(category, path),
        "center_of_mass_mm": infer_com(category, path),
        "motion": infer_motion(category, path, mass),
        "joint_constraints": infer_constraints(category, path),
    }


def material_label(material_key: str) -> str:
    return {
        "abs": "ABS engineering polymer",
        "acetal": "acetal engineering plastic",
        "alloy-steel": "alloy steel",
        "aluminum-6061": "6061-T6 aluminum",
        "bearing-steel": "chrome bearing steel",
        "brass": "machined brass",
        "fr4": "FR-4 fiberglass laminate",
        "galvanized-steel": "galvanized sheet steel",
        "hdpe": "HDPE polymer",
        "polycarbonate": "polycarbonate",
        "polyurethane": "cast polyurethane",
        "stainless-steel": "304 stainless steel",
        "structural-steel": "S235 structural steel",
        "tool-steel": "tool steel",
    }.get(material_key, material_key.replace("-", " "))


def procedural_dimensions(seed: int, shape: str) -> dict[str, Any]:
    fine_a = ((seed // 997) % 1000) / 1000.0
    fine_b = ((seed // 1597) % 1000) / 1000.0
    fine_c = ((seed // 2137) % 1000) / 1000.0
    fine_d = ((seed // 2879) % 1000) / 1000.0
    if shape == "cylinder":
        radius = 3.0 + ((seed * 7) % 95) / 2.0 + fine_a
        length = 4.0 + ((seed * 11) % 260) + fine_b
        return {
            "shape": "cylinder",
            "radius_mm": round(radius, 2),
            "length_mm": round(length, 2),
            "segments": 20 + (seed % 14) * 2,
        }
    if shape == "gear":
        teeth = 12 + (seed % 52)
        root_radius = 8.0 + ((seed * 5) % 90) / 2.0 + fine_a
        tooth_depth = 1.2 + ((seed * 3) % 18) / 10.0 + fine_b / 3.0
        thickness = 3.0 + ((seed * 13) % 60) + fine_c
        return {
            "shape": "gear",
            "teeth": teeth,
            "root_radius_mm": round(root_radius, 2),
            "outer_radius_mm": round(root_radius + tooth_depth, 2),
            "thickness_mm": round(thickness, 2),
        }
    if shape == "bracket":
        width = 14.0 + ((seed * 5) % 150) + fine_a
        height = 18.0 + ((seed * 7) % 180) + fine_b
        depth = 10.0 + ((seed * 11) % 90) + fine_c
        thickness = 3.0 + ((seed * 13) % 18) + fine_d / 2.0
        return {
            "shape": "bracket",
            "width_mm": round(width, 2),
            "height_mm": round(height, 2),
            "depth_mm": round(depth, 2),
            "thickness_mm": round(thickness, 2),
        }
    width = 8.0 + ((seed * 5) % 220) + fine_a
    depth = 6.0 + ((seed * 7) % 180) + fine_b
    height = 2.0 + ((seed * 11) % 90) + fine_c
    return {
        "shape": "box",
        "width_mm": round(width, 2),
        "depth_mm": round(depth, 2),
        "height_mm": round(height, 2),
    }


def procedural_volume_mm3(parameters: dict[str, Any]) -> float:
    shape = parameters["shape"]
    if shape == "cylinder":
        return 3.14159 * parameters["radius_mm"] ** 2 * parameters["length_mm"]
    if shape == "gear":
        root = parameters["root_radius_mm"]
        outer = parameters["outer_radius_mm"]
        teeth = parameters["teeth"]
        average_area = 3.14159 * ((root + outer) / 2.0) ** 2
        tooth_factor = 1.0 + min(0.22, teeth / 420.0)
        return average_area * parameters["thickness_mm"] * tooth_factor
    if shape == "bracket":
        w = parameters["width_mm"]
        h = parameters["height_mm"]
        d = parameters["depth_mm"]
        t = parameters["thickness_mm"]
        return (w * d * t) + (t * d * h)
    return parameters["width_mm"] * parameters["depth_mm"] * parameters["height_mm"]


def procedural_center_of_mass(parameters: dict[str, Any]) -> dict[str, Any]:
    shape = parameters["shape"]
    if shape == "cylinder":
        z = parameters["length_mm"] / 2.0
    elif shape == "gear":
        z = parameters["thickness_mm"] / 2.0
    else:
        z = parameters.get("height_mm", parameters.get("thickness_mm", 1.0)) / 2.0
    return {
        "x": 0.0,
        "y": 0.0,
        "z": round(z, 2),
        "coordinate_frame": "Procedural model origin at nominal base/axis center; +Z follows extrusion or thickness axis.",
    }


def procedural_physics(category: str, signature: str, parameters: dict[str, Any], material_key: str) -> dict[str, Any]:
    density = MATERIAL_DENSITIES[material_key]
    fill_factor = 0.72 if parameters["shape"] == "bracket" else 0.88 if parameters["shape"] == "gear" else 1.0
    mass = round(max(0.05, procedural_volume_mm3(parameters) * density * 1e-6 * fill_factor), 2)
    return {
        "mass_grams": mass,
        "material_composition": [
            {
                "material": material_label(material_key),
                "density_kg_m3": density,
            }
        ],
        "center_of_mass_mm": procedural_center_of_mass(parameters),
        "motion": infer_motion(category, signature, mass),
        "joint_constraints": infer_constraints(category, signature),
    }


def make_procedural_part(sequence: int, variant: int, existing_ids: set[str]) -> dict[str, Any]:
    category, series_name, shape, material_key = PROCEDURAL_SERIES[variant % len(PROCEDURAL_SERIES)]
    seed = 100000 + variant * 37 + sequence * 101
    parameters = procedural_dimensions(seed, shape)
    dimensions = "x".join(str(value) for key, value in parameters.items() if key.endswith("_mm"))
    base_name = f"{series_name} {dimensions} mm"
    part_id = slugify(f"procedural-{category}-{series_name}-{dimensions}-{sequence}")
    while part_id in existing_ids:
        sequence += 1
        part_id = slugify(f"procedural-{category}-{series_name}-{dimensions}-{sequence}")
    existing_ids.add(part_id)
    filename = f"{part_id}.stl"
    signature = f"{category}/{series_name}/{parameters}/{material_key}"
    return {
        "id": part_id,
        "name": base_name,
        "category": category,
        "summary": f"Procedural {series_name.lower()} digital twin with unique dimensions for simulation, prototyping, student competitions, and engineering fixtures.",
        "cad": {
            "format": "STL",
            "filename": filename,
            "download_url": f"/api/parts/{part_id}/cad",
            "source_page": f"/api/parts/{part_id}",
            "repository": "PhysixCAD procedural library",
            "license": PROCEDURAL_LICENSE,
            "source_type": "procedural",
            "generator": "physixcad-parametric-stl-v1",
            "parameters": parameters,
            "procedure_key": signature,
        },
        "media": {
            "image_url": None,
            "image_source": f"/api/parts/{part_id}/cad",
            "image_kind": "procedural-cad-preview",
            "alt": f"{base_name} generated CAD preview",
        },
        "competition_relevance": competition_tags(category, signature),
        "metadata_quality": {
            "mass": "calculated from procedural volume, material density, and shape fill factor",
            "center_of_mass": "calculated from procedural dimensions",
            "constraints": f"category template for {category}",
            "requires_vendor_review": True,
        },
        "physics": procedural_physics(category, signature, parameters, material_key),
    }


def image_for(path: str, images_by_dir: dict[str, list[str]]) -> tuple[str | None, str | None]:
    part_path = PurePosixPath(path)
    stem = part_path.stem.lower()
    directories = [str(part_path.parent), str(part_path.parent / "Images")]
    for directory in directories:
        for image_path in images_by_dir.get(directory, []):
            image_stem = PurePosixPath(image_path).stem.lower()
            if stem == image_stem or stem in image_stem or image_stem in stem:
                return image_path, "name-match"

    weak_preview_names = ("readme", "logo", "screenshot", "spreadsheet", "dimensions", "spec", "table", "markings")
    for directory in directories:
        candidates = images_by_dir.get(directory, [])
        strong = [candidate for candidate in candidates if not any(token in PurePosixPath(candidate).stem.lower() for token in weak_preview_names)]
        if strong:
            return strong[0], "directory-image"
        if candidates:
            return candidates[0], "directory-image"
    return None, None


def all_candidates(tree: dict[str, Any]) -> list[dict[str, Any]]:
    paths = [entry["path"] for entry in tree.get("tree", []) if entry.get("type") == "blob"]
    images = [
        path
        for path in paths
        if path.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".svg")) and not path.startswith("thumbnails/") and is_ascii(path)
    ]
    images_by_dir: dict[str, list[str]] = defaultdict(list)
    for image_path in images:
        images_by_dir[str(PurePosixPath(image_path).parent)].append(image_path)

    priority = {".step": 0, ".stp": 1, ".stl": 2}
    cad_paths: list[str] = []
    for path in paths:
        suffix = PurePosixPath(path).suffix.lower()
        if suffix not in priority or not is_ascii(path):
            continue
        if not path.startswith(ALLOWED_SOURCE_PREFIXES):
            continue
        cad_paths.append(path)

    candidates: list[dict[str, Any]] = []
    for cad_path in sorted(cad_paths, key=lambda item: (item.split("/")[0], PurePosixPath(item).parent.as_posix(), priority[PurePosixPath(item).suffix.lower()], item.lower())):
        category = classify_path(cad_path)
        if category not in CATEGORY_QUOTAS:
            continue
        image_path, image_kind = image_for(cad_path, images_by_dir)
        candidates.append(
            {
                "cad_path": cad_path,
                "image_path": image_path,
                "image_kind": image_kind or "generated-placeholder",
                "category": category,
                "name": pretty_name(cad_path),
            }
        )
    return candidates


def round_robin_select(candidates: list[dict[str, Any]], quota: int) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in sorted(candidates, key=lambda item: (item["image_kind"] != "name-match", item["cad_path"].lower())):
        groups[str(PurePosixPath(candidate["cad_path"]).parent)].append(candidate)

    selected: list[dict[str, Any]] = []
    while len(selected) < quota and groups:
        for group_key in sorted(list(groups)):
            group = groups[group_key]
            if not group:
                del groups[group_key]
                continue
            selected.append(group.pop(0))
            if len(selected) == quota:
                break
    return selected


def make_part(candidate: dict[str, Any], sequence: int) -> dict[str, Any]:
    cad_path = candidate["cad_path"]
    image_path = candidate["image_path"]
    name = candidate["name"]
    category = candidate["category"]
    part_id = slugify(f"{category}-{name}-{sequence}")
    suffix = PurePosixPath(cad_path).suffix.upper().lstrip(".")
    return {
        "id": part_id,
        "name": name,
        "category": category,
        "summary": f"{category} digital twin for student competition robots, ARC-style builds, shop fixtures, and professional engineering assemblies.",
        "cad": {
            "format": "STEP" if suffix == "STP" else suffix,
            "filename": PurePosixPath(cad_path).name,
            "download_url": encoded_url(RAW_BASE, cad_path),
            "source_page": encoded_url(BLOB_BASE, cad_path),
            "repository": "FreeCAD-library",
            "license": FREECAD_LICENSE,
        },
        "media": {
            "image_url": encoded_url(RAW_BASE, image_path) if image_path else None,
            "image_source": encoded_url(BLOB_BASE, image_path) if image_path else encoded_url(BLOB_BASE, cad_path),
            "image_kind": candidate["image_kind"],
            "alt": f"{name} preview image",
        },
        "competition_relevance": competition_tags(category, cad_path),
        "metadata_quality": metadata_quality(category),
        "physics": infer_physics(category, cad_path),
    }


def enrich_existing_part(part: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(part)
    enriched.setdefault("competition_relevance", competition_tags(part["category"], part["name"]))
    enriched.setdefault(
        "metadata_quality",
        {
            "mass": "reference value from common vendor/spec-sheet class",
            "center_of_mass": "nominal symmetric estimate",
            "constraints": "hand-authored MVP template",
            "requires_vendor_review": False,
        },
    )
    media = EXISTING_MEDIA.get(part["id"])
    if media:
        enriched["media"] = media
    return enriched


def build_expanded_database() -> dict[str, Any]:
    current = read_json(DATABASE_PATH)
    base_parts = [enrich_existing_part(part) for part in current["parts"][:5]]
    existing_downloads = {part["cad"]["download_url"] for part in base_parts}
    existing_filenames = {part["cad"]["filename"].lower() for part in base_parts}

    tree = load_freecad_tree()
    candidates = [
        candidate
        for candidate in all_candidates(tree)
        if encoded_url(RAW_BASE, candidate["cad_path"]) not in existing_downloads
        and PurePosixPath(candidate["cad_path"]).name.lower() not in existing_filenames
    ]

    selected: list[dict[str, Any]] = []
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        by_category[candidate["category"]].append(candidate)

    for category, quota in CATEGORY_QUOTAS.items():
        remaining = TARGET_ADDITIONAL_PARTS - len(selected)
        if remaining <= 0:
            break
        selected.extend(round_robin_select(by_category.get(category, []), min(quota, remaining)))

    if len(selected) < TARGET_ADDITIONAL_PARTS:
        selected_paths = {candidate["cad_path"] for candidate in selected}
        backfill_pool = [
            candidate
            for candidate in candidates
            if candidate["cad_path"] not in selected_paths and candidate["category"] in CATEGORY_QUOTAS
        ]
        selected.extend(round_robin_select(backfill_pool, TARGET_ADDITIONAL_PARTS - len(selected)))

    freecad_parts = [make_part(candidate, index + 1) for index, candidate in enumerate(selected)]
    procedural_parts: list[dict[str, Any]] = []
    if len(freecad_parts) < TARGET_ADDITIONAL_PARTS:
        existing_ids = {part["id"] for part in base_parts + freecad_parts}
        needed = TARGET_ADDITIONAL_PARTS - len(freecad_parts)
        procedural_parts = [
            make_procedural_part(len(freecad_parts) + index + 1, index, existing_ids)
            for index in range(needed)
        ]

    if len(freecad_parts) + len(procedural_parts) != TARGET_ADDITIONAL_PARTS:
        raise RuntimeError(
            f"Expected {TARGET_ADDITIONAL_PARTS} generated parts, got {len(freecad_parts) + len(procedural_parts)}"
        )

    expanded_parts = base_parts + freecad_parts + procedural_parts
    counts = Counter(part["category"] for part in expanded_parts)
    media_counts = {
        "source_images": sum(1 for part in expanded_parts if part.get("media", {}).get("image_url")),
        "generated_placeholders": sum(
            1 for part in expanded_parts if part.get("media", {}).get("image_kind") == "generated-placeholder"
        ),
        "procedural_previews": sum(
            1 for part in expanded_parts if part.get("media", {}).get("image_kind") == "procedural-cad-preview"
        ),
    }
    return {
        "catalog": {
            "name": "PhysixCAD Smart CAD Marketplace",
            "part_count": len(expanded_parts),
            "media_counts": media_counts,
            "public_source_count": len(base_parts) + len(freecad_parts),
            "procedural_source_count": len(procedural_parts),
            "source_repositories": ["FreeCAD-library", "Printables", "PhysixCAD procedural library"],
            "generated_by": "catalog_expander.py",
            "notes": [
                "New expanded entries use public FreeCAD-library CAD files; repository preview images are attached where available.",
                "Procedural entries are generated by PhysixCAD with unique dimensions, metadata, and local STL endpoints.",
                "Entries without source preview images use the frontend's generated industrial CAD placeholder.",
                "Auto-generated physics values are simulation bootstrap estimates and should be vendor-reviewed before safety-critical use.",
            ],
            "category_counts": dict(sorted(counts.items())),
        },
        "parts": expanded_parts,
    }


def main() -> None:
    data = build_expanded_database()
    write_json(DATABASE_PATH, data)
    print(f"Wrote {data['catalog']['part_count']} PhysixCAD parts to {DATABASE_PATH}")
    for category, count in data["catalog"]["category_counts"].items():
        print(f"- {category}: {count}")


if __name__ == "__main__":
    main()
