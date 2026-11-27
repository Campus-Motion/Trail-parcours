"""Exercise real FIT decoding and heatmap rendering through HTTP."""
import base64
import io
import struct

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

import api
import heatmap_v2

client = TestClient(api.app)


def crc(data):
    value = 0
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0xA001 if value & 1 else 0)
    return value


def fit_activity(*, latitude=46.5, stationary=False, health_field=False):
    # FIT record definition: timestamp, latitude, longitude (semicircles).
    data = struct.pack('<BBBH B', 0x40, 0, 0, 20, 4 if health_field else 3)
    data += bytes([253, 4, 0x86, 0, 4, 0x85, 1, 4, 0x85])
    if health_field:
        data += bytes([3, 1, 2])  # Heart rate; import must discard it.
    for index in range(5):
        lat = int(latitude / heatmap_v2.SEMICIRCLE)
        lon = int((6.5 + (0 if stationary else index * .0001)) / heatmap_v2.SEMICIRCLE)
        data += struct.pack('<BIii', 0, 1_000_000_000 + index * 5, lat, lon)
        if health_field:
            data += bytes([150])
    header = struct.pack('<BBHI4s', 14, 0x20, 2100, len(data), b'.FIT')
    header += struct.pack('<H', crc(header))
    contents = header + data
    return contents + struct.pack('<H', crc(contents))


def upload(contents=None, url='/heatmaps'):
    return client.post(url, files=[('files', ('activity.fit', fit_activity() if contents is None else contents, 'application/octet-stream'))])


def test_json_contains_rendered_png_and_geographic_bounds():
    response = upload()
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['runs'] == 1
    assert result['points'] == 5
    image = Image.open(io.BytesIO(base64.b64decode(result['image'].split(',')[1])))
    assert image.format == 'PNG'
    assert image.mode == 'RGBA'
    assert max(image.size) <= api.MAX_SIDE_PX
    assert np.asarray(image)[:, :, 3].max() > 0
    (south, west), (north, east) = result['bounds']
    assert south < 46.5 < north
    assert west < 6.5 < east


def test_multiple_uploads_are_independent_and_accumulated():
    response = client.post('/heatmaps', files=[
        ('files', ('same.fit', fit_activity())),
        ('files', ('same.fit', fit_activity())),
    ])
    assert response.status_code == 200
    assert response.json()['runs'] == 2
    assert response.json()['points'] == 10
    assert upload().json()['runs'] == 1


def test_html_map():
    response = upload(url='/heatmaps?output=html')
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/html')
    assert 'data:image/png;base64,' in response.text
    assert 'L.imageOverlay(' in response.text


def test_invalid_and_incomplete_fit_are_rejected():
    corrupted = bytearray(fit_activity())
    corrupted[-1] ^= 0xFF
    for payload in (b'', b'not a FIT file', fit_activity()[:-8], bytes(corrupted)):
        assert upload(payload).status_code == 422


def test_unusable_gps_and_stationary_tracks_are_rejected():
    assert upload(fit_activity(latitude=89)).status_code == 422
    assert upload(fit_activity(stationary=True)).status_code == 422


def test_limits_and_busy_renderer(monkeypatch):
    monkeypatch.setattr(api, 'MAX_TOTAL_BYTES', 1)
    assert upload().status_code == 413
    monkeypatch.setattr(api, 'MAX_TOTAL_BYTES', 10 * 1024 * 1024)
    monkeypatch.setattr(api, 'MAX_POINTS', 2)
    assert upload().status_code == 413
    api._render_lock.acquire()
    try:
        assert upload().status_code == 503
    finally:
        api._render_lock.release()


def test_validation_and_health():
    assert client.get('/health').json() == {'status': 'ok'}
    assert client.post('/heatmaps').status_code == 422
    assert upload(url='/heatmaps?output=unknown').status_code == 422
    assert client.post('/heatmaps', files={'files': ('x.txt', b'x')}).status_code == 422
    assert client.post('/heatmaps', files=[('files', ('x.fit', b''))] * 21).status_code == 413


def test_grid_limit_includes_blur_margin():
    t = np.array([0., 5., 10.])
    tracks = [(t, np.array([46., 46.1, 46.2]), np.array([6., 6.1, 6.2]))]
    heat, *_ = heatmap_v2.compute_heat(tracks, max_side_px=128)
    assert max(heat.shape) <= 128


def waypoint_request():
    return {'tracks': [{'points': [
        {'latitude': 46.5, 'longitude': 6.5 + i * .0001,
         'recorded_at': f'2026-11-12T12:00:{i * 5:02d}Z'}
        for i in range(5)
    ]}]}


def test_waypoints_use_the_same_renderer_as_fit():
    response = client.post('/heatmaps/waypoints', json=waypoint_request())
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['runs'] == 1 and result['points'] == 5
    image = Image.open(io.BytesIO(base64.b64decode(result['image'].split(',')[1])))
    assert np.asarray(image)[:, :, 3].max() > 0
    assert max(image.size) <= api.MAX_SIDE_PX
    # An out-of-order source still has chronological route segments.
    payload = waypoint_request()
    payload['tracks'][0]['points'].reverse()
    assert client.post('/heatmaps/waypoints', json=payload).json() == result


def test_waypoint_validation_and_limits(monkeypatch):
    for payload in ({'tracks': []}, {'tracks': [{'points': []}]}):
        assert client.post('/heatmaps/waypoints', json=payload).status_code == 422
    payload = waypoint_request()
    payload['tracks'][0]['points'][0]['latitude'] = 89
    assert client.post('/heatmaps/waypoints', json=payload).status_code == 422
    payload = waypoint_request()
    payload['tracks'][0]['points'][0]['recorded_at'] = '2026-11-12T12:00:00'
    assert client.post('/heatmaps/waypoints', json=payload).status_code == 422
    monkeypatch.setattr(api, 'MAX_POINTS', 3)
    assert client.post('/heatmaps/waypoints', json=waypoint_request()).status_code == 413


def test_waypoints_share_busy_lock_and_reject_stationary_data():
    api._render_lock.acquire()
    try:
        assert client.post('/heatmaps/waypoints', json=waypoint_request()).status_code == 503
    finally:
        api._render_lock.release()
    payload = waypoint_request()
    for point in payload['tracks'][0]['points']:
        point['longitude'] = 6.5
    assert client.post('/heatmaps/waypoints', json=payload).status_code == 422



def test_fit_import_parser_returns_only_absolute_gps_records():
    response = client.post('/fit/parse', files={'file': ('run.fit', fit_activity(health_field=True))})
    assert response.status_code == 200, response.text
    result = response.json()
    assert list(result) == ['waypoints']
    assert len(result['waypoints']) == 5
    first = result['waypoints'][0]
    assert set(first) == {'latitude', 'longitude', 'recorded_at'}
    assert abs(first['latitude'] - 46.5) < 1e-6
    assert first['recorded_at'].endswith('Z')
    # The decoder's output can be passed directly to the existing renderer.
    rendered = client.post('/heatmaps/waypoints', json={'tracks': [{'points': result['waypoints']}]})
    assert rendered.status_code == 200
    assert rendered.json()['points'] == 5


def test_fit_import_rejects_corruption_missing_gps_and_limits(monkeypatch):
    for payload in [b'', b'invalid', fit_activity()[:-1]]:
        assert client.post('/fit/parse', files={'file': ('run.fit', payload)}).status_code == 422
    assert client.post('/fit/parse', files={'file': ('run.txt', fit_activity())}).status_code == 422
    assert client.post('/fit/parse', files={'file': ('run.fit', fit_activity(latitude=89))}).status_code == 422
    monkeypatch.setattr(api, 'MAX_POINTS', 3)
    assert client.post('/fit/parse', files={'file': ('run.fit', fit_activity())}).status_code == 413
    monkeypatch.setattr(api, 'MAX_TOTAL_BYTES', 2)
    assert client.post('/fit/parse', files={'file': ('run.fit', fit_activity())}).status_code == 413
