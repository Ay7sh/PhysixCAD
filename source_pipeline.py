from __future__ import annotations

import argparse
import json
import ssl
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DATABASE_PATH = ROOT / "database.json"


def load_database(path: Path = DATABASE_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def save_database(data: dict[str, Any], path: Path = DATABASE_PATH) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def identify_standard_cad_models(path: Path = DATABASE_PATH, limit: int | None = None) -> list[dict[str, Any]]:
    """Return public CAD sources used by the PhysixCAD catalog."""
    data = load_database(path)
    sources: list[dict[str, Any]] = []
    parts = data["parts"][:limit] if limit else data["parts"]
    for part in parts:
        cad = part["cad"]
        sources.append(
            {
                "part_id": part["id"],
                "name": part["name"],
                "category": part["category"],
                "cad_format": cad["format"],
                "download_url": cad["download_url"],
                "source_page": cad["source_page"],
                "image_url": part.get("media", {}).get("image_url"),
                "repository": cad["repository"],
                "license": cad["license"],
                "source_type": cad.get("source_type", "remote"),
                "generator": cad.get("generator"),
            }
        )
    return sources


def validate_source_url(url: str, timeout_seconds: float = 8.0) -> dict[str, Any]:
    """Validate a CAD/source URL without downloading the whole model when possible."""
    if url.startswith("/api/parts/") and url.endswith("/cad"):
        return {
            "ok": True,
            "status": "generated",
            "content_type": "model/stl",
            "content_length": None,
            "elapsed_ms": 0,
            "content_valid": True,
            "source_type": "procedural",
        }

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return {"ok": False, "status": None, "error": "Unsupported URL scheme"}

    started = time.monotonic()
    headers = {
        "User-Agent": "PhysixCAD-MVP/1.0 (+https://physixcad.local)",
        "Accept": "application/octet-stream,text/plain,application/zip,*/*",
    }
    for method in ("HEAD", "GET"):
        try:
            request = Request(url, method=method, headers=headers)
            if method == "GET":
                request.add_header("Range", "bytes=0-2047")
            try:
                response_context = urlopen(request, timeout=timeout_seconds)
                certificate_fallback = False
            except URLError as exc:
                if not isinstance(exc.reason, ssl.SSLCertVerificationError):
                    raise
                response_context = urlopen(
                    request,
                    timeout=timeout_seconds,
                    context=ssl._create_unverified_context(),
                )
                certificate_fallback = True
            with response_context as response:
                content_type = response.headers.get("content-type")
                content_length = response.headers.get("content-length")
                content_valid = not (content_type or "").lower().startswith("text/html")
                return {
                    "ok": 200 <= response.status < 400 and content_valid,
                    "status": response.status,
                    "content_type": content_type,
                    "content_length": content_length,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "certificate_verification_fallback": certificate_fallback,
                    "content_valid": content_valid,
                }
        except HTTPError as exc:
            if method == "HEAD" and exc.code in {403, 405}:
                continue
            return {
                "ok": False,
                "status": exc.code,
                "error": str(exc.reason),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
        except (TimeoutError, URLError) as exc:
            if method == "HEAD":
                continue
            return {
                "ok": False,
                "status": None,
                "error": str(exc),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }

    return {"ok": False, "status": None, "error": "Validation failed"}


def run_pipeline(validate: bool = False, limit: int | None = None) -> dict[str, Any]:
    sources = identify_standard_cad_models(limit=limit)
    if validate:
        for source in sources:
            source["validation"] = validate_source_url(source["download_url"])

    data = load_database()
    return {
        "source_count": len(sources),
        "catalog_count": len(data["parts"]),
        "categories": data.get("catalog", {}).get("category_counts", {}),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sources": sources,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Identify PhysixCAD MVP public CAD sources.")
    parser.add_argument("--validate", action="store_true", help="Validate CAD download URLs.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a compact text table.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Limit the number of sources emitted or validated.")
    args = parser.parse_args()

    result = run_pipeline(validate=args.validate, limit=args.limit)
    if args.json:
        print(json.dumps(result, indent=2))
        return

    print(f"PhysixCAD CAD source pipeline: {result['source_count']} models")
    for source in result["sources"]:
        status = ""
        if "validation" in source:
            validation = source["validation"]
            status = f" [{validation.get('status') or 'n/a'} {'ok' if validation.get('ok') else 'check'}]"
        print(f"- {source['name']} -> {source['download_url']}{status}")


if __name__ == "__main__":
    main()
