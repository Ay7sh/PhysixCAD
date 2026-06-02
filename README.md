# PhysixCAD MVP

PhysixCAD is a FastAPI prototype for a B2B marketplace of "Smart CAD" digital twins: downloadable CAD geometry bundled with simulation-ready physics metadata.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

Open <http://127.0.0.1:8000>.

## API

- `GET /api/parts` returns the full 10,000-part marketplace catalog, category list, media metadata, CAD links, and physics metadata.
- `GET /api/parts/{part_id}` returns one digital twin.
- `GET /api/parts/{part_id}/smart-package` downloads a ZIP with CAD plus `physics/metadata.json`.
- `GET /api/source-pipeline?limit=20` lists public CAD sources.
- `GET /api/source-pipeline?validate=true&limit=10` validates a limited set of remote CAD URLs.

## Catalog Expansion

The original five hero models are preserved. `catalog_expander.py` builds a 10,000-model catalog from 5,107 public-source CAD entries plus 4,893 PhysixCAD procedural CAD entries. The procedural entries are deterministic STL models with unique dimensions, unique local CAD endpoints, and category-specific physics metadata. This is 5,000 more models than the previous 5,000-part version.

- Actuators and motors
- Bearings and linear motion
- Electronics and control
- Fasteners and hardware
- Architecture and BIM
- HVAC and fluid systems
- Industrial design and products
- Logistics and packaging
- Medical and biomedical
- Pneumatics and fluid systems
- Power transmission
- Robotics assemblies
- Sensors and switches
- Sports engineering
- Structural framing
- Tooling and fixtures

The current generated catalog contains 689 source-backed preview images, 4,418 generated placeholders for public CAD entries without images, and 4,893 procedural CAD previews. The marketplace remains fast by loading cards in batches.

Regenerate the expanded catalog:

```bash
python catalog_expander.py
```

## CAD Sources

The catalog uses public sources including the FreeCAD-library and Printables. Remote hosts can rate-limit automated downloads, so smart packages include source provenance and a small fallback STL preview if a host is unavailable during the request.

Auto-generated metadata is intended as a simulation bootstrap. Vendor-certified masses, load ratings, and safety constraints should be reviewed before safety-critical use.
# PhysixCAD
