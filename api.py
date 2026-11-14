"""HTTP interface for the existing FIT heatmap renderer."""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Annotated, Literal

import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

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
                path = Path(directory) / f"{index}.fit"
                total_bytes += save_fit_upload(upload, path, MAX_TOTAL_BYTES - total_bytes)
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
        return render_tracks(tracks, output)
    finally:
        _render_lock.release()


def render_tracks(tracks, output="json"):
    heat, peak, bounds, _, pixel_m = heatmap.compute_heat(tracks, max_side_px=MAX_SIDE_PX)
    if not np.any(heat > 0):
        raise HTTPException(422, "No moving segments remain after time and speed filtering.")
    image, saturation = heatmap.render_base64(heat, peak)
    if output == "html":
        result = heatmap.build_map(bounds, image, len(tracks), saturation, pixel_m)
        return HTMLResponse(result.get_root().render())
    return HeatmapResult(image=f"data:image/png;base64,{image}", bounds=bounds,
                         runs=len(tracks), points=sum(len(track[0]) for track in tracks),
                         pixel_m=pixel_m, saturation=saturation)


class GpsPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    latitude: float = Field(gt=-85, lt=85, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    recorded_at: AwareDatetime


class GpsTrack(BaseModel):
    model_config = ConfigDict(extra="forbid")
    points: list[GpsPoint] = Field(min_length=2, max_length=MAX_POINTS)


class GpsHeatmapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tracks: list[GpsTrack] = Field(min_length=1, max_length=MAX_FILES)


@app.post("/heatmaps/waypoints", response_model=HeatmapResult)
def create_waypoint_heatmap(request: GpsHeatmapRequest):
    """Internal backend interface. Coordinates are processed without persistence."""
    if sum(len(track.points) for track in request.tracks) > MAX_POINTS:
        raise HTTPException(413, f"Combined activities exceed {MAX_POINTS} GPS points.")
    if not _render_lock.acquire(blocking=False):
        raise HTTPException(503, "Renderer busy; retry later.", headers={"Retry-After": "2"})
    try:
        tracks = []
        for track in request.tracks:
            points = sorted(track.points, key=lambda point: point.recorded_at)
            origin = points[0].recorded_at
            tracks.append((
                np.array([(point.recorded_at - origin).total_seconds() for point in points]),
                np.array([point.latitude for point in points]),
                np.array([point.longitude for point in points]),
            ))
        return render_tracks(tracks)
    finally:
        _render_lock.release()


def save_fit_upload(upload, path, max_bytes):
    if not (upload.filename or "").lower().endswith(".fit"):
        raise HTTPException(422, "Every upload must have a .fit extension.")
    size = 0
    with path.open("wb") as target:
        while chunk := upload.file.read(64 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(413, "Combined FIT payload exceeds 10 MiB.")
            target.write(chunk)
    return size


class FitActivity(BaseModel):
    waypoints: list[GpsPoint] = Field(min_length=2, max_length=MAX_POINTS)


@app.post("/fit/parse", response_model=FitActivity)
def parse_fit(file: Annotated[UploadFile, File()]):
    """Internal import decoder. Return GPS only; discard the uploaded FIT file."""
    with TemporaryDirectory(prefix="trail-fit-import-") as directory:
        path = Path(directory) / "activity.fit"
        save_fit_upload(file, path, MAX_TOTAL_BYTES)
        try:
            points = heatmap.read_fit_points(path, strict=True, max_points=MAX_POINTS)
            return FitActivity(waypoints=[
                GpsPoint(latitude=lat, longitude=lon, recorded_at=timestamp)
                for timestamp, lat, lon in points
            ])
        except heatmap.FitPointLimitError as exc:
            raise HTTPException(413, "FIT activity exceeds the GPS point limit.") from exc
        except Exception as exc:
            raise HTTPException(422, "Invalid FIT activity or insufficient supported GPS data.") from exc
