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
