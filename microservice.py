import argparse
import asyncio
import threading
import json
import requests
from flask import Flask, request, jsonify, abort

from NovaApi.ListDevices.nbe_list_devices import request_device_list
from ProtoDecoders.decoder import parse_device_list_protobuf, get_canonic_ids, parse_device_update_protobuf
from NovaApi.ExecuteAction.LocateTracker.location_request import create_location_request
from NovaApi.nova_request import nova_request
from NovaApi.scopes import NOVA_ACTION_API_SCOPE
from NovaApi.util import generate_random_uuid
from Auth.fcm_receiver import FcmReceiver
from NovaApi.ExecuteAction.LocateTracker.decrypt_locations import extract_locations

app = Flask(__name__)
API_TOKEN = None
PUSH_URL = None
periodic_jobs = {}
PERSISTENCE_FILE = 'periodic_jobs.json'


def _load_jobs_from_disk():
    try:
        with open(PERSISTENCE_FILE, 'r') as f:
            data = json.load(f)
        return {str(k): float(v) for k, v in data.items()}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _save_jobs_to_disk():
    data = {device_id: job.interval for device_id, job in periodic_jobs.items()}
    try:
        with open(PERSISTENCE_FILE, 'w') as f:
            json.dump(data, f)
    except Exception:
        pass


def _restore_jobs():
    jobs = _load_jobs_from_disk()
    for device_id, interval in jobs.items():
        try:
            job = PeriodicUploader(device_id, interval)
            periodic_jobs[device_id] = job
            job.start()
        except Exception:
            pass
    _save_jobs_to_disk()


def _require_bearer_token():
    """
    Enforce an Authorization: Bearer <token> header.
    Falls back to 401 if header is missing / malformed / wrong.
    """
    auth_header = request.headers.get('Authorization', '')
    # Expect exactly:  "Bearer <token-value>"
    scheme, _, token = auth_header.partition(' ')
    if scheme.lower() != 'bearer' or not token or token != API_TOKEN:
        abort(401, description='Invalid or missing bearer token')


@app.before_request
def before_request():
    _require_bearer_token()


@app.route('/devices', methods=['GET'])
def list_devices():
    result_hex = request_device_list()
    device_list = parse_device_list_protobuf(result_hex)
    canonic_ids = get_canonic_ids(device_list)
    devices = [{'name': name, 'id': cid} for name, cid in canonic_ids]
    return jsonify({'devices': devices})


def _ensure_event_loop():
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)


def _fetch_location(device_id):
    _ensure_event_loop()

    result = None
    request_uuid = generate_random_uuid()

    def handler(resp_hex):
        nonlocal result
        update = parse_device_update_protobuf(resp_hex)
        if update.fcmMetadata.requestUuid == request_uuid:
            result = update

    fcm_token = FcmReceiver().register_for_location_updates(handler)
    payload = create_location_request(device_id, fcm_token, request_uuid)
    nova_request(NOVA_ACTION_API_SCOPE, payload)

    while result is None:
        asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.1))

    return extract_locations(result)


def _get_latest_location(locations):
    # Pick the location with the latest timestamp that has coordinates
    with_coords = [l for l in locations if 'latitude' in l and 'longitude' in l]
    if not with_coords:
        return None
    return max(with_coords, key=lambda l: l.get('time', 0))


@app.route('/devices/<device_id>/location', methods=['GET'])
def get_device_location(device_id):
    locations = _fetch_location(device_id)
    return jsonify({'locations': locations})


def _upload_location(device_id, location):
    if not PUSH_URL:
        raise RuntimeError('Push service URL not configured')
    if not location:
        raise RuntimeError('No valid location')
    data = {
        'id': device_id,
        'lat': location['latitude'],
        'long': location['longitude'],
    }
    try:
        requests.post(PUSH_URL, data=data, timeout=10)
    except Exception:
        pass


@app.route('/devices/<device_id>/position-single', methods=['POST'])
def push_position_single(device_id):
    locations = _fetch_location(device_id)
    location = _get_latest_location(locations)
    if not location:
        abort(404, description='No location available')
    _upload_location(device_id, location)
    return jsonify({'status': 'uploaded'})


class PeriodicUploader:
    def __init__(self, device_id, interval):
        self.device_id = device_id
        self.interval = interval
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        self._thread.join()

    def _run(self):
        while not self._stop_event.is_set():
            try:
                locations = _fetch_location(self.device_id)
                location = _get_latest_location(locations)
                if location:
                    _upload_location(self.device_id, location)
            except Exception:
                pass
            if self._stop_event.wait(self.interval):
                break


@app.route('/devices/<device_id>/position-periodic', methods=['POST'])
def start_periodic_upload(device_id):
    try:
        interval = float(request.args.get('interval', '0'))
    except ValueError:
        abort(400, description='Invalid interval')
    if interval <= 0:
        abort(400, description='Interval must be > 0')
    if device_id in periodic_jobs:
        abort(400, description='Job already running')

    job = PeriodicUploader(device_id, interval)
    periodic_jobs[device_id] = job
    job.start()
    _save_jobs_to_disk()
    return jsonify({'status': 'started', 'interval': interval})


@app.route('/devices/<device_id>/position-stop', methods=['POST'])
def stop_periodic_upload(device_id):
    job = periodic_jobs.pop(device_id, None)
    if not job:
        abort(404, description='No running job for device')
    job.stop()
    _save_jobs_to_disk()
    return jsonify({'status': 'stopped'})


def main():
    parser = argparse.ArgumentParser(description="Google Find Hub Sync")
    parser.add_argument('--auth-token', required=True, help='Bearer token that clients must supply')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=5500)
    parser.add_argument('--push-url', help='URL to upload locations to')
    args = parser.parse_args()

    global API_TOKEN
    global PUSH_URL
    API_TOKEN = args.auth_token
    PUSH_URL = args.push_url

    _restore_jobs()

    app.run(host=args.host, port=args.port)


if __name__ == '__main__':
    main()
