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
        settings = helper.load(self.settings_path)
        result["auto_switch"] = settings.get("auto_switch") is True
        result["reopen_game"] = settings.get("reopen_game") is True
        result["pending_games"] = await asyncio.to_thread(helper.load_pending_games) if result["reopen_game"] else []
        return result

    def _write_settings(self, values: dict):
        helper.save(self.settings_path, {**helper.load(self.settings_path), **values})

    async def _set_flag(self, key: str, enabled: bool) -> dict:
        if type(enabled) is not bool:
            return {"ok": False, "error": "Expected a boolean"}
        async with self.lock:
            try:
                await asyncio.to_thread(self._write_settings, {key: enabled})
                return {"ok": True}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

    async def set_auto_switch(self, enabled: bool):
        return await self._set_flag("auto_switch", enabled)

    async def set_reopen_game(self, enabled: bool):
        result = await self._set_flag("reopen_game", enabled)
        if result["ok"] and not enabled:
            # Don't leave a stale game queued after the toggle is turned off.
            await asyncio.to_thread(helper.clear_pending_games)
        return result

    async def run_action(self, action: str):
        if not isinstance(action, str) or action not in {"switch", "eject", "eject-sleep", "restart"}:
            return {"ok": False, "error": "Unknown action"}
        async with self.lock:
            try:
                if action == "eject-sleep" and helper.load(self.settings_path).get("reopen_game") is True:
                    # Snapshot while games are still running; the eject below closes them.
                    try:
                        games = await asyncio.to_thread(helper.snapshot_games_for_relaunch)
                        decky.logger.info(f"Snapshotted for reopen: {games}")
                    except Exception:
                        decky.logger.exception("Game snapshot failed, ejecting without a reopen queue")
                        await asyncio.to_thread(helper.clear_pending_games)
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
            if not resumed:
                continue
            settings = helper.load(self.settings_path)
            if settings.get("auto_switch") is not True and settings.get("reopen_game") is not True:
                continue
            # CLOCK_BOOTTIME includes suspend; MONOTONIC excludes it. This
            # catches normal Steam/power-button sleep without installing hooks.
            try:
                if settings.get("auto_switch") is True:
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
                if helper.load(self.settings_path).get("reopen_game") is True:
                    if await asyncio.to_thread(helper.load_pending_games):
                        message = await asyncio.to_thread(helper.relaunch_pending_games, self.uid)
                        decky.logger.info(message)
            except Exception:
                decky.logger.exception("Resume handling failed")
            previous = sleep_offset()
