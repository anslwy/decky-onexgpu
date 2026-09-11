from __future__ import annotations

import asyncio
from contextlib import suppress
import importlib.util
import os
from pathlib import Path
import pwd
import time

import decky

# Decky does not guarantee that the plugin directory is on sys.path.
spec = importlib.util.spec_from_file_location("decky_onexgpu_helper", Path(__file__).with_name("helper.py"))
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def sleep_offset() -> float:
    return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()


class Plugin:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.watcher = None
        self.settings_path = Path(os.environ.get("DECKY_PLUGIN_SETTINGS_DIR", "/home/deck/homebrew/settings/decky-onexgpu")) / "settings.json"
        self.uid = pwd.getpwnam(os.environ.get("DECKY_USER", "deck")).pw_uid

    async def _main(self):
        self.watcher = asyncio.create_task(self._watch_resume())
        decky.logger.info("decky-onexgpu started (standalone backend)")

    async def _unload(self):
        if self.watcher:
            self.watcher.cancel()
            with suppress(asyncio.CancelledError):
                await self.watcher
        # The systemd worker intentionally survives plugin/session reloads.

    async def get_status(self):
        result = await asyncio.to_thread(helper.status)
        result["auto_switch"] = helper.load(self.settings_path).get("auto_switch") is True
        return result

    async def set_auto_switch(self, enabled: bool):
        if type(enabled) is not bool:
            return {"ok": False, "error": "Expected a boolean"}
        async with self.lock:
            try:
                await asyncio.to_thread(helper.save, self.settings_path, {"auto_switch": enabled})
                return {"ok": True}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

    async def run_action(self, action: str):
        if not isinstance(action, str) or action not in {"switch", "eject", "eject-sleep", "restart"}:
            return {"ok": False, "error": "Unknown action"}
        async with self.lock:
            try:
                await asyncio.to_thread(helper.launch, action, self.uid)
                return {"ok": True}
            except Exception as exc:
                decky.logger.exception("GPU operation could not be started")
                return {"ok": False, "error": str(exc)}

    async def _watch_resume(self):
        previous = sleep_offset()
        while True:
            await asyncio.sleep(2)
            current = sleep_offset()
            resumed = current - previous > 0.5
            previous = current
            if not resumed or helper.load(self.settings_path).get("auto_switch") is not True:
                continue
            # CLOCK_BOOTTIME includes suspend; MONOTONIC excludes it. This
            # catches normal Steam/power-button sleep without installing hooks.
            try:
                for _ in range(30):
                    if helper.load(self.settings_path).get("auto_switch") is not True:
                        break
                    if not await asyncio.to_thread(helper.busy):
                        # A removed GPU may need a PCI rescan after wake.
                        result = await self.run_action("switch")
                        if not result["ok"]:
                            decky.logger.warning(result["error"])
                        break
                    await asyncio.sleep(2)
            except Exception:
                decky.logger.exception("Auto-switch on wake failed")
            previous = sleep_offset()
