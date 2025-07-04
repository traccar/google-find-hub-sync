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
API_KEY = None


def _require_api_key():
    key = request.args.get('api_key') or request.headers.get('X-API-Key')
    if API_KEY and key != API_KEY:
        abort(401, description="Invalid API key")


@app.before_request
def before_request():
    _require_api_key()


@app.route('/devices', methods=['GET'])
def list_devices():
    result_hex = request_device_list()
    device_list = parse_device_list_protobuf(result_hex)
    canonic_ids = get_canonic_ids(device_list)
    devices = [{'name': name, 'id': cid} for name, cid in canonic_ids]
    return jsonify({'devices': devices})


def _fetch_location(device_id):
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
    parser = argparse.ArgumentParser(description="Device microservice")
    parser.add_argument('--api-key', required=True, help='API key to secure the endpoints')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=5000)
    args = parser.parse_args()

    global API_KEY
    API_KEY = args.api_key

    app.run(host=args.host, port=args.port)


if __name__ == '__main__':
    main()
