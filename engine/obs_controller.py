"""Thin wrapper around OBS WebSocket (protocol v5) via obsws-python.

Used by engine/demo_bot.py and engine/test_obs.py to drive OBS recording
during automated demo capture.
"""

import os
import subprocess
import time
from pathlib import Path

import obsws_python as obs
from dotenv import load_dotenv

load_dotenv()

CHROME_CAPTURE_SOURCE = "Chrome Capture"
VSCODE_CAPTURE_SOURCE = "VSCode Capture"
DISPLAY_CAPTURE_SOURCE = "Display Capture"


def ensure_chrome_foreground() -> None:
    """Bring the Chrome window to the foreground via PowerShell AppActivate.

    Display Capture records the whole monitor regardless of focus, but
    Chrome still needs to be on top for the demo to look right on screen.
    """
    subprocess.run(
        [
            "powershell",
            "-Command",
            "$wshell = New-Object -ComObject wscript.shell; "
            "$wshell.AppActivate('Google Chrome')",
        ],
        creationflags=subprocess.CREATE_NO_WINDOW,
        capture_output=True,
    )
    time.sleep(0.5)


class OBSController:
    """Minimal OBS WebSocket v5 client for start/stop recording control."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        password: str | None = None,
        timeout: int = 3,
    ):
        self.host = host or os.getenv("OBS_WEBSOCKET_HOST", "localhost")
        self.port = int(port or os.getenv("OBS_WEBSOCKET_PORT", "4455"))
        self.password = password if password is not None else os.getenv("OBS_WEBSOCKET_PASSWORD", "")
        self.timeout = timeout
        self.client: obs.ReqClient | None = None

    def connect(self) -> None:
        """Connect and authenticate with the OBS WebSocket server."""
        self.client = obs.ReqClient(
            host=self.host, port=self.port, password=self.password, timeout=self.timeout
        )

    def get_version(self) -> dict:
        """Return OBS/obs-websocket version info."""
        response = self.client.get_version()
        return {
            "obs_version": response.obs_version,
            "obs_web_socket_version": response.obs_web_socket_version,
        }

    def get_current_scene(self) -> str:
        """Return the name of the currently active program scene."""
        return self.client.get_current_program_scene().current_program_scene_name

    def set_output_path(self, directory: Path) -> None:
        """Set the directory OBS will save recordings to.

        OBS WebSocket v5 does not support setting a full output filename
        directly; OBS generates the filename itself. Callers should rename
        the file returned by stop_recording() afterwards.
        """
        directory.mkdir(parents=True, exist_ok=True)
        self.client.set_record_directory(str(directory))

    def start_recording(self) -> None:
        """Start OBS recording the current scene."""
        self.client.start_record()

    def stop_recording(self) -> str | None:
        """Stop OBS recording and return the path of the saved file."""
        return self.client.stop_record().output_path

    def disconnect(self) -> None:
        """Close the OBS WebSocket connection."""
        if self.client is not None:
            self.client.base_client.ws.close()
            self.client = None

    def _set_capture_enabled(self, scene_name: str, source_name: str, enabled: bool) -> None:
        item_id = self.client.get_scene_item_id(scene_name, source_name).scene_item_id
        self.client.set_scene_item_enabled(scene_name, item_id, enabled)

    def add_display_capture(self) -> None:
        """Ensure a single Display Capture source exists and is enabled.

        Removes any leftover Chrome/VSCode window capture sources -- those
        go black when the captured window loses focus during unattended
        recording, which Display Capture does not suffer from.
        """
        scene_name = self.get_current_scene()

        existing_inputs = {item["inputName"] for item in self.client.get_input_list().inputs}
        for source_name in (CHROME_CAPTURE_SOURCE, VSCODE_CAPTURE_SOURCE):
            if source_name in existing_inputs:
                self.client.remove_input(source_name)
                print(f"[OBS] Removed {source_name}")

        scene_items = self.client.get_scene_item_list(scene_name).scene_items
        existing = {item["sourceName"] for item in scene_items}

        if DISPLAY_CAPTURE_SOURCE in existing:
            print(f"[OBS] {DISPLAY_CAPTURE_SOURCE} already exists")
            self._set_capture_enabled(scene_name, DISPLAY_CAPTURE_SOURCE, True)
        else:
            self.client.create_input(
                sceneName=scene_name,
                inputName=DISPLAY_CAPTURE_SOURCE,
                inputKind="monitor_capture",
                inputSettings={"monitor": 0},
                sceneItemEnabled=True,
            )
            print(f"[OBS] {DISPLAY_CAPTURE_SOURCE} source added")
