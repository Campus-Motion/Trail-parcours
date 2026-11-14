# Trail-parcours

Trail GPS visualisation with a FastAPI service around `heatmap_v2.py`.
The original command-line renderer remains available; `onecourse.py` is independent.

## Run the API (Python 3.12+)

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/uvicorn api:app --host 0.0.0.0 --port 3041
```

Interactive API documentation: `/docs`; OpenAPI schema: `/openapi.json`.
`GET /health` returns `{"status":"ok"}`.

## Generate a heatmap

`POST /heatmaps` accepts multipart form data with one or more `files` fields.
Each request generates a heatmap from exactly those activities. Files are processed
in a temporary directory and deleted; there is no database or persistent history.

```sh
curl --fail-with-body http://localhost:3041/heatmaps \
  -F 'files=@run1.fit' -F 'files=@run2.fit' > heatmap.json

curl --fail-with-body 'http://localhost:3041/heatmaps?output=html' \
  -F 'files=@run1.fit' -o heatmap.html
```

The default JSON response contains:

| Field | Meaning |
| --- | --- |
| `image` | PNG data URL (`data:image/png;base64,...`) |
| `bounds` | `[[south, west], [north, east]]` in degrees (WGS84) |
| `runs`, `points` | Number of activities and decoded GPS records |
| `pixel_m` | Actual raster resolution in metres per pixel |
| `saturation` | Equivalent passage intensity at colour saturation |

For a Leaflet frontend, display the result with
`L.imageOverlay(result.image, result.bounds).addTo(map)` and
`map.fitBounds(result.bounds)`. For an embedded web view, request `output=html`.
The HTML map loads Leaflet assets and Esri tiles over the Internet; generation
of the JSON overlay itself does not contact these services.

Limits: 20 files, 10 MiB combined FIT contents, 100,000 GPS points and a raster
of at most 1024 pixels per side (resolution automatically decreases for large
areas). The script's GPS smoothing, time-gap and speed filters remain in use.
Invalid files, missing GPS, unsupported coordinates and tracks with no accepted
moving segments return HTTP 422; size/count limits return 413; a busy renderer
returns 503 with `Retry-After`. Missing files and invalid output options return 422.

The FIT content limit applies while reading already-parsed multipart uploads.
Configure a total HTTP request-body limit and timeouts at the reverse proxy too.

## Container deployment

```sh
docker build -t trail-heatmap .
docker run --rm -p 127.0.0.1:3041:3041 trail-heatmap
```

This follows the separate heatmap service in the project diagrams. Put your
HTTPS reverse proxy on port 443 in front of this internal service on port 3041.
The image runs as an unprivileged user. Live processes must restart after an
environment restore.

This initial API has no authentication or database integration. Keep it private
until the gateway enforces authentication, request limits and rate limits.
CORS is disabled by default: route browser API calls through the same origin.
The database schema and user access rules are not defined in this repository;
connect them in a subsequent integration step.

## Tests

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Tests generate a valid synthetic FIT activity with checksums and exercise real
FIT decoding, PNG rendering, HTML output, multiple uploads and error responses.
No personal GPS data is bundled.

## Campus Motion app integration

`POST /heatmaps/waypoints` is the internal JSON interface used by
`Campus-Motion/Back_End_App`. It accepts:

```json
{"tracks":[{"points":[
  {"latitude":46.5,"longitude":6.5,"recorded_at":"2026-11-12T12:00:00Z"},
  {"latitude":46.5,"longitude":6.5001,"recorded_at":"2026-11-12T12:00:05Z"}
]}]}
```

The result has the same JSON shape as the FIT endpoint. Timestamps require an
explicit UTC offset; points are sorted chronologically. Limits are 20 tracks,
100,000 total points, and 1024 pixels per side. The original FIT API remains
available. The endpoint contains no authentication: run it only on a private
network, behind the backend's authenticated `GET /activities/heatmap` endpoint.
The backend selects only the current user's routes and sends no identity or JWT.
No GPS or generated heatmaps are persisted. Both endpoints share the render lock.

The backend Compose file builds this repository as the internal `heatmap`
service when both repositories are checked out as sibling directories. Its
`HEATMAP_URL` should be `http://heatmap:3041`. The app displays the returned PNG
without loading third-party tiles. Deploy the three matching repository changes
together; no production deployment is performed by adding these files.

For cloud builds that require the session CA, the Dockerfile supports an optional
BuildKit secret: `docker build --secret id=proxy_ca,src=/path/to/ca-bundle.pem .`.
The CA is mounted only during dependency installation and is not stored in image
layers. Ordinary builds do not require this secret.

### Internal FIT decoder for saved activities

`POST /fit/parse` accepts one multipart `file` (a `.fit` file, at most 10 MiB)
and returns `{"waypoints":[{"latitude":46.5,"longitude":6.5,
"recorded_at":"2026-11-12T12:00:00Z"}, ...]}`. It shares the renderer's FIT
reader and enforces CRC validation, at least two supported GPS points and at
most 100,000 points. Sensor/health fields are ignored. The input file is removed
at the end of the request; the parser stores no activities or user identity.

The backend's authenticated `POST /activities/import-fit` calls this endpoint,
then atomically saves a private activity and its waypoints. `GET
/activities/heatmap` subsequently renders those saved points. Raw-file retention
and third-party destinations are not required. This is an internal endpoint;
retain private-network isolation and let the backend enforce user authentication.
