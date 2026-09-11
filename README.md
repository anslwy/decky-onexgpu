# decky-onexgpu

A minimal **Decky Loader** plugin focused on **ONEXGPU 2 / AMD eGPUs**, with a standalone Python backend. No all-ways-egpu installation or setup is required.

## The seven fields

1. **Eject eGPU then sleep** — eject first, suspend only after successful removal, restart the session on wake.
2. **Reopen game after resume** — toggle, off by default. Relaunch the running Steam game after resume (see below).
3. **Eject eGPU** — select the internal GPU, stop the session, unbind/remove GPU and HDMI audio, restart on the internal GPU.
4. **Switch to eGPU** — rescan if necessary, select the eGPU, restart the graphical session.
5. **Status** — detected GPU model, PCI address, selected primary flag, driver power cap, queued reopen, and operation result.
6. **Auto-switch on wake** — persistent, off by default. Wait for an in-flight eject to finish, rescan/wait up to 20 seconds for the eGPU, and switch after resume.
7. **Restart Display Manager** — session recovery.

### Reopen game after resume

When the toggle is on, **Eject eGPU then sleep** records the running Steam games (via their `SteamAppId`) before the session stops. After resume — and after the automatic switch when that is also on — the helper waits up to two minutes for the Steam client to come back, then relaunches the recorded games with `steam://rungameid/<id>` as your user. Status shows the queued game as "Will reopen".

Limits, stated plainly: this reopens the game, it does not restore where you were — unsaved progress is lost, so save in-game first. It covers Steam games only, and relaunching needs the Steam client to return on its own after the session restart. Turning the toggle off discards any queued game.

The two eject buttons appear only while an eGPU is connected. **Switch to eGPU** appears when the eGPU is disconnected or is not selected as primary. These actions stay hidden until the first status response and are disabled during an operation.

**Switching, ejecting and display recovery close running games and restart the graphical session.** GPU eject leaves the enclosure's USB, Ethernet and storage devices attached. Unmount enclosure storage separately before physically unplugging it.

## Requirements and scope

- Linux with systemd, AMD `amdgpu`, `/usr/bin/python3` (3.9+), util-linux `mount`/`umount`, and Decky Loader with root plugins enabled.
- Primary target: CachyOS handheld Gaming Mode with an internal AMD GPU and ONEXGPU 2. Other distributions/session implementations need hardware validation.
- Detection uses hot-removable PCI topology or the ONEXGPU 2 profile observed on the target: `1002:747e`, subsystem `2014:8018`. Internal-panel GPUs are excluded. Multiple matching GPUs are refused. Model names come from sysfs or libdrm's device/revision database, with `lspci` and numeric IDs as fallbacks. On this device, `747e` revision `d8` resolves to **AMD Radeon RX 7800M**; revision `c8` identifies the RX 7800 XT. PCI device IDs alone cannot distinguish them.
- The implementation uses temporary, read-only bind mounts over `boot_vga`, then restarts the display manager and the host user's systemd session. It does not unload the shared `amdgpu` module. “Selected as primary” reports the selection flag, not proof of which GPU every game uses.
- Selections are transient and reset on reboot. Auto-switch operates while Decky's backend is running; it detects sleep longer than 0.5 seconds using Linux clocks. It does not auto-eject on ordinary power-button sleep. Use **Eject eGPU then sleep** for that sequence.
- A systemd worker outlives Steam and plugin reloads. Operations are serialized; session recovery runs in Python `finally` and systemd `ExecStopPost`, with a 180-second runtime bound. Kernel/driver hangs can still require a reboot.
- PCI rescan cannot fix every USB4 tunnel or OCuLink hotplug failure. If a removed GPU does not return, replug/power-cycle it. Unknown Thunderbolt devices must be authorized through the OS first.

### Turbo findings

Rechecked on 2026-09-11: the [official driver/support page](https://onexplayerstore.com/pages/drivers-and-faqs) links ONEXGPU 2 to AMD's RX 7800M driver, without documenting an enclosure-control API. [DROIX's hardware review](https://droix.net/blogs/onexplayer-onexgpu-2-review/) describes the physical button switching between 130 W and 180 W. No documented software toggle or reliable button-state readout was found in the sources reviewed or the inspected Linux interfaces. This is not proof that an undocumented interface cannot exist.

The target exposes a **180 W cap/default/max** and a **162 W minimum**, plus standard AMD workload profiles. Neither is a validated turbo-state flag. USB/HID enumeration also includes an unidentified `0416:8002` device with a vendor-defined feature report; its identity and relationship to the enclosure are unverified. No undocumented reports were sent. Correlating read-only measurements with physical button changes would be needed before showing an on/off status. Version 0.1.1 removes all turbo UI, including the disabled toggle and unavailable text; the correctly labelled driver power cap remains.

## Build and install

```sh
npm ci
npm run typecheck
npm test
npm run zip
```

The installable archive is **`out/decky-onexgpu.zip`**. In Decky, enable developer mode and install the ZIP through its local plugin installer. Alternatively, extract its `decky-onexgpu/` directory into `~/homebrew/plugins/` as root and restart `plugin_loader.service`.

The archive includes only the runtime files, metadata and documentation. Settings live in Decky's `DECKY_PLUGIN_SETTINGS_DIR/settings.json`; transient operation results and selection files live in `/run/decky-onexgpu/`.

### GitHub releases

In GitHub, open **Actions → Release plugin → Run workflow**, select the branch, and run it to publish the release immediately. The action builds the current `package.json` version and attaches `decky-onexgpu.zip` to a release named `v<version>`. Check **Create a draft release** first if you want to review it before publishing instead.

Alternatively, pushing a matching version tag publishes the release automatically:

```sh
git tag v0.1.2
git push origin v0.1.2
```

Before a new release, update the version in `package.json` and `package-lock.json`, commit, and push. Tags must match the package version. Existing releases are not overwritten, and a tag pointing at a different commit is rejected. The workflow runs typechecks and backend tests before building, and also saves the ZIP as a workflow artifact. It uses GitHub's built-in token; no additional secrets are required. Releases and artifacts in the private repository require repository access.

### Migrating from egpu-switch / all-ways-egpu

Clear old boot-VGA/compositor overrides using all-ways-egpu's own cleanup commands, and disable its automatic switching services before using this plugin. The helper detects common conflicting services, bind mounts and compositor files and reports them rather than overwriting them. Do not run the two switching plugins concurrently. all-ways-egpu may then be uninstalled; it is never invoked by this plugin.

### Diagnostics and removal

From the installed plugin directory:

```sh
python3 helper.py status
journalctl -u decky-onexgpu-operation.service -b
# Recover a stopped graphical session:
sudo systemctl start user@1000.service display-manager.service
# Restore original boot-VGA flags and restart the session before uninstalling:
sudo python3 helper.py restore --uid 1000 --launch
```

Replace `1000` with `id -u` if needed. Wait for an operation to finish, turn off Auto-switch on wake, restore selection (or reboot), then remove the plugin through Decky. No persistent system services or sleep hooks are installed.

## Credits

Inspired by [TiPSilva/egpu-switch](https://github.com/TiPSilva/egpu-switch) and the boot-VGA technique documented in [all-ways-egpu](https://github.com/ewagner12/all-ways-egpu). This is a new, smaller implementation, not a wrapper around either project. MIT licensed.

## Verification — 2026-09-11

Validated on the supplied CachyOS handheld (kernel `7.2.3-1-cachyos-deckify`, Decky `v3.2.8`, Gamescope `3.16.25`):

- TypeScript typecheck, Rollup production build and 19 backend unit tests passed.
- Decky loaded the installed plugin backend successfully.
- Live eGPU selection, GPU/audio PCI eject, PCI rescan and re-selection succeeded.
- Gamescope's journal confirmed the renderer changed from `AMD Ryzen Z1 Extreme (RADV PHOENIX)` on the internal session to `AMD Radeon RX 7800M (RADV NAVI32)` after the automatic eGPU switch.
- An RTC-timed sleep test verified the full eject → suspend → resume → automatic switch sequence with the real Decky watcher. The final eject/sleep and switch workers both exited successfully; display-manager, user session and Decky remained active.
- The test caught and fixed a systemd resume race: the kernel wakes before `user.slice` is thawed. The helper now waits for thaw before restarting the session and retries frozen-unit recovery.
- DisplayPort audio reported `monitor_present=1` and `eld_valid=1` after reconnecting. This checks endpoint enumeration, not audible playback or game performance.
- The auto-switch preference was restored to its default off state after the test.

`tests/hardware_sleep.py` is an optional **disruptive** root-only integration check for this installation layout. It sets a 30-second RTC alarm, closes the graphical session and suspends the device. Its journal check rejects intermediate failures even if a later switch succeeds. UI rendering/controller navigation still needs an on-device visual check; no screenshot-based verification was performed.
