import asyncio
import json
import sys
from datetime import datetime

import websockets


def describe_train(train):
    """One-line summary of a window record"""
    when = train.get('window_time') or '?'
    when = when[11:16] if len(when) >= 16 else when
    action = {True: "passes", False: "stops"}.get(train.get('passes_without_stopping'), "seen")
    origin = (train.get('origin') or {}).get('name') or '?'
    destination = (train.get('destination') or {}).get('name') or '?'
    line = (f"{when} ({train.get('window_time_source')}) {train.get('headcode') or train.get('rid')} "
            f"{action}, {train.get('direction') or 'direction unknown'} | {origin} -> {destination}")
    if train.get('category_description'):
        line += f" | {train['category_description']}"
    if train.get('platform'):
        line += f" | platform {train['platform']}"
    if train.get('length'):
        line += f" | {train['length']} cars"
    if train.get('cancelled'):
        line += " | CANCELLED"
    looks = train.get('appearance')
    if looks:
        line += f"\n        {looks['confidence'].upper()}: {looks['summary']}"
        if looks.get('photo'):
            line += f"\n        Photo: {looks['photo'].get('page_url')}"
    return line

def describe_allocation(allocation):
    lines = [f"  Allocation ({allocation.get('match_method')}): {allocation.get('operator_name', 'N/A')}, "
             f"fleet {allocation.get('formation', {}).get('fleet_id', 'N/A')}"]
    if allocation.get('summary'):
        lines.append(f"  Summary: {allocation['summary'].get('formation', 'N/A')}")
    for unit in (allocation.get('rolling_stock') or {}).get('units', []):
        livery = unit.get('livery') or {}
        lines.append(f"  Unit {unit['unit_number']}: {livery.get('code')} - {livery.get('description')}")
        if unit.get('photos'):
            lines.append(f"    Photo: {unit['photos'][0]['page_url']}")
    return "\n".join(lines)

async def test_websocket(verbose=False):
    uri = "ws://localhost:8767"

    print(f"Connecting to WebSocket server at {uri}...")

    try:
        async with websockets.connect(uri) as websocket:
            print("✓ Connected successfully!")
            print("Waiting for train updates...")
            print("=" * 60)

            while True:
                try:
                    message = await websocket.recv()

                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        print(f"Raw message: {message}")
                        continue

                    if verbose:
                        print(json.dumps(data, indent=2))

                    if data.get('type') == 'window_board':
                        print(f"\n=== Upcoming trains past the window ({datetime.now():%H:%M:%S}) ===")
                        for train in data['data']:
                            print(describe_train(train))
                        if not data['data']:
                            print("(none known yet)")

                    elif data.get('type') == 'train_update':
                        train = data['data']
                        print(f"\n--- Update at {datetime.now():%H:%M:%S} ---")
                        print(describe_train(train))
                        if train.get('allocation'):
                            print(describe_allocation(train['allocation']))
                        else:
                            print("  ⚠ No allocation data matched")

                    else:
                        print(f"{data.get('type')}: {data.get('message', '')}")

                except websockets.exceptions.ConnectionClosed:
                    print("Connection closed by server")
                    break

    except ConnectionRefusedError:
        print("❌ Could not connect to WebSocket server")
        print("Make sure the backend is running on localhost:8767")
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    print("WebSocket Test Client (pass --verbose to print full JSON)")
    print("=" * 60)
    asyncio.run(test_websocket(verbose='--verbose' in sys.argv))
