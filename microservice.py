import argparse
import asyncio
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


@app.route('/devices/<device_id>/location', methods=['GET'])
def get_device_location(device_id):
    locations = _fetch_location(device_id)
    return jsonify({'locations': locations})


def main():
    parser = argparse.ArgumentParser(description="Google Find Hub Sync")
    parser.add_argument('--auth-token', required=True, help='Bearer token that clients must supply')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=5500)
    args = parser.parse_args()

    global API_TOKEN
    API_TOKEN = args.auth_token

    app.run(host=args.host, port=args.port)


if __name__ == '__main__':
    main()
