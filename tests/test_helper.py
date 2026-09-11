import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("helper", Path(__file__).resolve().parents[1] / "helper.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class HardwareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sys = self.root / "sys"
        self.run = self.root / "run"
        self.patch_sys = patch.object(h, "SYS", self.sys)
        self.patch_run = patch.object(h, "RUN", self.run)
        self.patch_sys.start()
        self.patch_run.start()
        self.addCleanup(self.patch_sys.stop)
        self.addCleanup(self.patch_run.stop)

    def device(self, bdf, cls="0x030000", onex=False, panel=False):
        path = self.sys / "bus/pci/devices" / bdf
        path.mkdir(parents=True)
        attrs = {"class": cls, "vendor": "0x1002", "device": "0x747e" if onex else "0x15bf",
                 "subsystem_vendor": "0x2014" if onex else "0x17aa",
                 "subsystem_device": "0x8018" if onex else "0x0000", "boot_vga": "0" if onex else "1"}
        for name, value in attrs.items():
            (path / name).write_text(value)
        (path / "drm/card0").mkdir(parents=True)
        if panel:
            (path / "drm/card0/card0-eDP-1").mkdir()
        driver = self.sys / "bus/pci/drivers/amdgpu"
        driver.mkdir(parents=True, exist_ok=True)
        (path / "driver").symlink_to(driver)
        return path

    def test_profile_detects_egpu_without_boot_vga_guess(self):
        self.device("0000:68:00.0", onex=True)
        self.device("0000:c3:00.0", panel=True)
        devices = h.gpus()
        self.assertEqual(h.choose_egpu(devices)["bdf"], "0000:68:00.0")
        self.assertEqual(h.choose_internal(devices)["bdf"], "0000:c3:00.0")
        self.assertFalse(h.choose_egpu(devices)["selected"])

    def test_internal_discrete_not_assumed_external(self):
        self.device("0000:01:00.0")
        self.assertIsNone(h.choose_egpu(h.gpus()))

    def test_model_distinguishes_mobile_from_desktop_revision(self):
        path = self.device("0000:68:00.0", onex=True)
        database = self.root / "amdgpu.ids"
        database.write_text("# IDs\n1.0.0\nbad,row,data\n747E, C8, AMD Radeon RX 7800 XT\n747E, D8, AMD Radeon RX 7800M\n")
        gpu = h.choose_egpu(h.gpus())
        with patch.object(h, "AMD_IDS", (database,)), patch.object(h, "command") as cmd:
            (path / "revision").write_text("0xd8")
            self.assertEqual(h.gpu_model(gpu), "AMD Radeon RX 7800M")
            (path / "revision").write_text("0xc8")
            self.assertEqual(h.gpu_model(gpu), "AMD Radeon RX 7800 XT")
            cmd.assert_not_called()

    def test_model_falls_back_to_pci_name_when_database_absent(self):
        self.device("0000:68:00.0", onex=True)
        with patch.object(h, "AMD_IDS", ()), patch.object(h, "command", return_value="Slot:\t0000:68:00.0\nDevice:\tNavi 32 [Radeon RX 7700 XT / 7800 XT]\n"):
            self.assertEqual(h.gpu_model(h.choose_egpu(h.gpus())), "Navi 32 [Radeon RX 7700 XT / 7800 XT]")

    def test_missing_model_tools_does_not_break_status_name(self):
        self.device("0000:68:00.0", onex=True)
        with patch.object(h, "AMD_IDS", ()), patch.object(h, "command", side_effect=FileNotFoundError):
            self.assertEqual(h.gpu_model(h.choose_egpu(h.gpus())), "AMD GPU (1002:747e)")

    def test_ambiguous_egpu_refused(self):
        self.device("0000:68:00.0", onex=True)
        self.device("0000:69:00.0", onex=True)
        with self.assertRaisesRegex(RuntimeError, "Multiple"):
            h.choose_egpu(h.gpus())

    def test_eject_only_gpu_and_audio_not_dock_storage(self):
        self.device("0000:68:00.0", onex=True)
        self.device("0000:68:00.1", "0x040300")
        self.device("0000:69:00.0", "0x010802")
        paths = h.eject_functions(h.choose_egpu(h.gpus()))
        self.assertEqual([p.name for p in paths], ["0000:68:00.1", "0000:68:00.0"])

    def test_unknown_sibling_blocks_eject(self):
        self.device("0000:68:00.0", onex=True)
        self.device("0000:68:00.2", "0x0c0330")
        with self.assertRaisesRegex(RuntimeError, "Unexpected"):
            h.eject_functions(h.choose_egpu(h.gpus()))

    def test_unbind_failure_recovers_dm_and_does_not_sleep(self):
        self.device("0000:68:00.0", onex=True)
        self.device("0000:c3:00.0", panel=True)
        with patch.object(h, "conflict", return_value=None), patch.object(h, "command") as cmd, \
                patch.object(h, "set_primary"), patch.object(h, "boot_mounts", return_value=[]), \
                patch.object(h, "write_sysfs", side_effect=RuntimeError("unbind failed")), \
                patch.object(h, "suspend_and_wait") as sleep:
            with self.assertRaisesRegex(RuntimeError, "unbind failed"):
                h.operate("eject-sleep", 1000)
            sleep.assert_not_called()
            self.assertTrue(any(c.args[:3] == ("systemctl", "start", "user@1000.service") for c in cmd.call_args_list))
            self.assertIn("unbind failed", h.load(self.run / "result.json")["error"])

    def test_missing_internal_fails_before_session_stop(self):
        self.device("0000:68:00.0", onex=True)
        with patch.object(h, "conflict", return_value=None), patch.object(h, "command") as cmd:
            with self.assertRaisesRegex(RuntimeError, "internal"):
                h.operate("eject", 1000)
            cmd.assert_not_called()

    def test_failed_mount_rolls_back(self):
        self.device("0000:68:00.0", onex=True)
        self.device("0000:c3:00.0", panel=True)
        devices = h.gpus()
        with patch.object(h, "clear_primary") as clear, patch.object(h, "command", side_effect=RuntimeError("mount failed")):
            with self.assertRaisesRegex(RuntimeError, "mount failed"):
                h.set_primary(h.choose_egpu(devices), devices)
            self.assertEqual(clear.call_count, 2)

    def test_only_owned_mounts_are_removed(self):
        text = "42 1 0:1 /decky-onexgpu/1 /sys/devices/a/boot_vga ro - tmpfs tmpfs ro\n43 1 0:2 /all-ways-egpu/1 /sys/devices/b/boot_vga ro - ext4 /dev/root ro"
        with patch.object(h, "read", return_value=text), patch.object(h, "command") as cmd:
            h.clear_primary()
            cmd.assert_called_once_with("umount", "--", "/sys/devices/a/boot_vga")

    def test_busy_worker_rejects_second_launch(self):
        with patch.object(h, "busy", return_value=True), patch.object(h, "command") as cmd:
            with self.assertRaisesRegex(RuntimeError, "already"):
                h.launch("switch", 1000)
            cmd.assert_not_called()

    def test_suspend_waits_for_resume_not_just_enqueue(self):
        with patch.object(h.time, "CLOCK_BOOTTIME", 7, create=True), \
                patch.object(h, "command", side_effect=["", "frozen", "active", "running", "inactive"]) as cmd, patch.object(h.time, "monotonic", return_value=10), \
                patch.object(h.time, "clock_gettime", side_effect=[10, 10, 20]), patch.object(h.time, "sleep") as wait:
            h.suspend_and_wait()
            self.assertEqual(cmd.call_args_list[0].args, ("systemctl", "suspend"))
            self.assertEqual(wait.call_count, 2)

    def test_display_recovery_retries_frozen_user_session(self):
        with patch.object(h, "command", side_effect=[RuntimeError("Cannot perform operation on frozen unit user@1000.service."), ""]) as cmd, patch.object(h.time, "sleep"):
            h.start_session(1000)
            self.assertEqual(cmd.call_count, 2)

    def fake_proc(self, pid, environ=None, name="game"):
        path = self.root / "proc" / pid
        path.mkdir(parents=True)
        if environ is not None:
            (path / "environ").write_bytes(environ)
        (path / "comm").write_text(name)
        return path

    def test_snapshot_finds_games_and_skips_client(self):
        self.fake_proc("100", b"HOME=/x\0SteamAppId=1234\0", "eldenring")
        self.fake_proc("200", b"SteamAppId=0\0", "steam")
        self.fake_proc("300", b"PATH=/usr/bin\0", "other")
        self.fake_proc("400", b"SteamAppId=1234\0", "eldenring-dup")
        with patch.object(h, "PROC", self.root / "proc"):
            games = h.snapshot_games_for_relaunch()
            self.assertEqual(games, [{"appid": 1234, "name": "eldenring"}])
            self.assertEqual(h.load_pending_games(), games)

    def test_relaunch_empty_pending_is_noop(self):
        with patch.object(h, "command") as cmd:
            self.assertEqual(h.relaunch_pending_games(1000), "No game to reopen.")
            cmd.assert_not_called()

    def test_relaunch_waits_for_steam_then_launches(self):
        import os
        h.save(self.run / "pending_relaunch.json", {"games": [{"appid": 570, "name": "dota"}]})
        calls = {"pgrep": 0}

        def fake_command(*args, **kwargs):
            if args[0] == "pgrep":
                calls["pgrep"] += 1
                return "" if calls["pgrep"] < 2 else "999"
            if args[0] == "runuser":
                self.assertIn("steam://rungameid/570", args)
                return ""
            raise AssertionError(f"unexpected command {args[0]}")

        with patch.object(h, "command", side_effect=fake_command) as cmd, patch.object(h.time, "sleep"):
            message = h.relaunch_pending_games(os.getuid(), wait_seconds=6)
            self.assertIn("Reopened dota", message)
            self.assertIn("saved in-game", message)
            self.assertTrue(any(c.args[0] == "runuser" for c in cmd.call_args_list))
            self.assertEqual(h.load_pending_games(), [])
            self.assertIn("Reopened dota", h.load(self.run / "result.json")["message"])

    def test_relaunch_gives_up_when_steam_never_returns(self):
        h.save(self.run / "pending_relaunch.json", {"games": [{"appid": 570, "name": "dota"}]})
        with patch.object(h, "command", return_value=""), patch.object(h.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "did not come back"):
                h.relaunch_pending_games(1000, wait_seconds=2)
            self.assertEqual(len(h.load_pending_games()), 1)


if __name__ == "__main__":
    unittest.main()
