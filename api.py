"""HTTP interface for the existing FIT heatmap renderer."""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Annotated, Literal

import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import heatmap_v2 as heatmap

MAX_FILES = 20
MAX_TOTAL_BYTES = 10 * 1024 * 1024
MAX_POINTS = 100_000
MAX_SIDE_PX = 1024
# Bound concurrent raster work per worker; do not queue expensive requests.
_render_lock = Lock()
app = FastAPI(title="Trail heatmap API", version="1.0.0")


class HeatmapResult(BaseModel):
    image: str
    bounds: list[list[float]]
    runs: int
    points: int
    pixel_m: float
    saturation: float


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/heatmaps", response_model=HeatmapResult,
          responses={200: {"content": {"text/html": {}}}})
def create_heatmap(
    files: Annotated[list[UploadFile], File(description="One or more FIT activities")],
    output: Literal["json", "html"] = "json",
):
    """Generate an independent heatmap; uploaded files are not retained."""
    if not 1 <= len(files) <= MAX_FILES:
        raise HTTPException(413, f"Upload between 1 and {MAX_FILES} FIT files.")
    if not _render_lock.acquire(blocking=False):
        raise HTTPException(503, "Renderer busy; retry later.", headers={"Retry-After": "2"})
    try:
        tracks = []
        total_bytes = total_points = 0
        with TemporaryDirectory(prefix="trail-heatmap-") as directory:
            for index, upload in enumerate(files):
                if not (upload.filename or "").lower().endswith(".fit"):
                    raise HTTPException(422, "Every upload must have a .fit extension.")
                # Server-generated names prevent path traversal and collisions.
                path = Path(directory) / f"{index}.fit"
                with path.open("wb") as target:
                    while chunk := upload.file.read(64 * 1024):
                        total_bytes += len(chunk)
                        if total_bytes > MAX_TOTAL_BYTES:
                            raise HTTPException(413, "Combined FIT payload exceeds 10 MiB.")
                        target.write(chunk)
                try:
                    # Parse separately so that one invalid activity cannot be silently skipped.
                    parsed = heatmap.load_tracks(path.parent, strict=True)
                except ValueError as exc:
                    raise HTTPException(422, f"Upload {index + 1} is not a valid FIT activity.") from exc
                finally:
                    path.unlink()
                if len(parsed) != 1:
                    raise HTTPException(422, f"Upload {index + 1} needs at least two timestamped GPS points.")
                t, lat, lon = parsed[0]
                if (not all(np.isfinite(a).all() for a in (t, lat, lon))
                        or np.any(np.abs(lat) >= 85) or np.any(np.abs(lon) > 180)):
                    raise HTTPException(422, "GPS coordinates must be finite, latitude within (-85, 85) and longitude within [-180, 180].")
                total_points += len(t)
                if total_points > MAX_POINTS:
                    raise HTTPException(413, f"Combined activities exceed {MAX_POINTS} GPS points.")
                tracks.extend(parsed)
        heat, peak, bounds, _, pixel_m = heatmap.compute_heat(tracks, max_side_px=MAX_SIDE_PX)
        if not np.any(heat > 0):
            raise HTTPException(422, "No moving segments remain after time and speed filtering.")
        image, saturation = heatmap.render_base64(heat, peak)
        if output == "html":
            result = heatmap.build_map(bounds, image, len(tracks), saturation, pixel_m)
            return HTMLResponse(result.get_root().render())
        return HeatmapResult(image=f"data:image/png;base64,{image}", bounds=bounds,
                             runs=len(tracks), points=total_points,
                             pixel_m=pixel_m, saturation=saturation)
    finally:
        _render_lock.release()
