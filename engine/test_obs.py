"""Smoke test for the OBS WebSocket connection.

Usage:
    python engine/test_obs.py

OBS must be running with the WebSocket server enabled (see
demo/obs_setup_guide.md).
"""

from dotenv import load_dotenv

load_dotenv()

import sys
import traceback

sys.stdout.reconfigure(encoding="utf-8")

from obs_controller import DISPLAY_CAPTURE_SOURCE, OBSController


def main() -> None:
    obs = OBSController()

    try:
        obs.connect()
    except Exception:
        print(f"Failed to connect to OBS WebSocket at {obs.host}:{obs.port}")
        traceback.print_exc()
        print()
        print("Make sure OBS is open and running, with the WebSocket server")
        print("enabled (Tools -> WebSocket Server Settings) on port 4455, and")
        print("that OBS_WEBSOCKET_PASSWORD in .env matches the server password.")
        print("See demo/obs_setup_guide.md for setup instructions.")
        sys.exit(1)

    try:
        version = obs.get_version()
        scene = obs.get_current_scene()
        print(f"OBS version: {version.get('obs_version')}")
        print(f"obs-websocket version: {version.get('obs_web_socket_version')}")
        print(f"Current scene: {scene}")

        obs.add_display_capture()
        scene_items = obs.client.get_scene_item_list(scene).scene_items
        sources = {item["sourceName"]: item for item in scene_items}

        if DISPLAY_CAPTURE_SOURCE in sources:
            item = sources[DISPLAY_CAPTURE_SOURCE]
            enabled = item["sceneItemEnabled"]
            print(f"{'✅' if enabled else '❌'} {DISPLAY_CAPTURE_SOURCE} present in scene (enabled: {enabled})")

            transform = obs.client.get_scene_item_transform(scene, item["sceneItemId"]).scene_item_transform
            width = transform.get("sourceWidth")
            height = transform.get("sourceHeight")
            print(f"   Capture dimensions: {width}x{height}")
        else:
            print(f"❌ {DISPLAY_CAPTURE_SOURCE} present in scene")

        print("OBS CONNECTION CONFIRMED")
    except Exception:
        print("Connected, but a request failed.")
        traceback.print_exc()
        sys.exit(1)
    finally:
        obs.disconnect()


if __name__ == "__main__":
    main()
