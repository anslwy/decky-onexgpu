"""Optional live integration check; run as root on the handheld with plugin installed.

Closes the graphical session, ejects the GPU, sleeps with an RTC wake alarm,
and verifies Decky's auto-switch watcher. Restores the previous setting.
"""
import importlib.util
import json
from pathlib import Path
import subprocess
import time

PLUGIN = Path("/home/deck/homebrew/plugins/decky-onexgpu")
SETTINGS = Path("/home/deck/homebrew/settings/decky-onexgpu/settings.json")
spec = importlib.util.spec_from_file_location("helper", PLUGIN / "helper.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
previous = h.load(SETTINGS)
started = str(int(time.time()))
try:
    h.save(SETTINGS, {"auto_switch": True})
    subprocess.run(["rtcwake", "-m", "no", "-s", "30"], check=True)
    h.launch("eject-sleep", 1000)
    # MONOTONIC excludes time suspended. The real Decky watcher must launch
    # the subsequent switch; this test never invokes it itself.
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        time.sleep(2)
        state = h.status()
        if not state["busy"] and state["error"]:
            raise RuntimeError(state["error"])
        if not state["busy"] and state["selected"] and state["message"] == "eGPU selected; graphical session restarted.":
            journal = subprocess.check_output(["journalctl", "-u", h.UNIT, "--since", "@" + started,
                                               "--no-pager", "-o", "cat"], text=True)
            if "Main process exited" in journal or "Traceback" in journal:
                raise RuntimeError("An intermediate worker failed:\n" + journal)
            print(json.dumps(state, indent=2), flush=True)
            break
    else:
        raise RuntimeError("Timed out waiting for Decky's auto-switch on wake")
finally:
    h.save(SETTINGS, previous)
    subprocess.run(["rtcwake", "-m", "disable"], check=False)
