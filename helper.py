"""Standalone Linux GPU operations. Also runs outside Decky's Python runtime."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import time

SYS = Path("/sys")
PROC = Path("/proc")
RUN = Path("/run/decky-onexgpu")
UNIT = "decky-onexgpu-operation.service"
BDF = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
ACTIONS = {"switch", "eject", "eject-sleep", "restart", "restore"}
AMD_IDS = (Path("/usr/share/libdrm/amdgpu.ids"), Path("/usr/share/hwdata/amdgpu.ids"),
           Path("/usr/local/share/libdrm/amdgpu.ids"))


def read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value))
    temp.replace(path)


def clean_env() -> dict:
    env = os.environ.copy()
    env.pop("LD_LIBRARY_PATH", None)
    if "LD_LIBRARY_PATH_ORIG" in env:
        env["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH_ORIG"]
    return env


def command(*args: str, timeout: int = 30, check: bool = True) -> str:
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=clean_env())
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Do not wait indefinitely for a sysfs writer stuck in kernel D-state.
        proc.kill()
        raise RuntimeError(f"{args[0]} timed out; recovery may require a reboot")
    if check and proc.returncode:
        raise RuntimeError((err or out).strip() or f"{args[0]} exited {proc.returncode}")
    return out.strip()


def write_sysfs(path: Path, value: str):
    command("/usr/bin/python3", "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])",
            str(path), value, timeout=20)


def gpus() -> list[dict]:
    found = []
    for path in sorted((SYS / "bus/pci/devices").glob("*")):
        if not BDF.fullmatch(path.name) or read(path / "class")[:4] != "0x03":
            continue
        vendor, device = read(path / "vendor"), read(path / "device")
        # PCI removable on the device or an ancestor identifies a USB4/PCIe
        # hotplug GPU. The observed ONEXGPU 2 subsystem also supports OCuLink.
        topology = [path.resolve(), *path.resolve().parents]
        external = any(read(p / "removable") == "removable" for p in topology)
        onex_profile = (vendor, device, read(path / "subsystem_vendor"),
                        read(path / "subsystem_device")) == ("0x1002", "0x747e", "0x2014", "0x8018")
        internal_panel = any(p.name.split("-")[1].startswith(("eDP", "LVDS", "DSI"))
                             for p in (path / "drm").glob("card*/card*-*") if "-" in p.name)
        cards = [p for p in (path / "drm").glob("card*") if re.fullmatch(r"card\d+", p.name)]
        found.append({"bdf": path.name, "vendor": vendor, "device": device,
                      "external": (external or onex_profile) and not internal_panel,
                      "onex_profile": onex_profile, "internal_panel": internal_panel,
                      "ready": bool(cards) and (path / "driver").resolve().name == "amdgpu",
                      "selected": read(path / "boot_vga") == "1"})
    return found


def choose_egpu(devices: list[dict]) -> dict | None:
    supported = [g for g in devices if g["external"] and g["vendor"] == "0x1002"]
    preferred = [g for g in supported if g["onex_profile"]]
    candidates = preferred or supported
    if len(candidates) > 1:
        raise RuntimeError("Multiple supported eGPUs found; connect only the intended ONEXGPU 2")
    return candidates[0] if candidates else None


def choose_internal(devices: list[dict]) -> dict:
    candidates = [g for g in devices if not g["external"] and g["ready"]]
    preferred = [g for g in candidates if g["internal_panel"]] or candidates
    if len(preferred) != 1:
        raise RuntimeError("Cannot identify one ready internal AMD GPU for eject")
    return preferred[0]


def boot_mounts() -> list[tuple[Path, bool]]:
    mounts = []
    for line in read(Path("/proc/self/mountinfo")).splitlines():
        fields = line.split()
        if len(fields) > 5 and fields[4].endswith("/boot_vga"):
            owned = fields[3] in ("/decky-onexgpu/0", "/decky-onexgpu/1",
                                  "/run/decky-onexgpu/0", "/run/decky-onexgpu/1")
            mounts.append((Path(fields[4]), owned))
    return mounts


def conflict() -> str | None:
    if any(not owned for _, owned in boot_mounts()):
        return "Another tool owns boot-VGA overrides. Clear those overrides before switching."
    for name in ("all-ways-egpu", "all-ways-egpu-boot-vga", "all-ways-egpu-set-compositor", "all-ways-egpu-user"):
        state = command("systemctl", "is-enabled", name + ".service", check=False)
        if state in {"enabled", "enabled-runtime", "linked", "linked-runtime"}:
            return f"Disable {name}.service before using standalone switching."
    for name in ("00-vulkan-device.conf", "10kwin.conf", "10sway.conf"):
        if Path("/etc/environment.d", name).exists():
            return f"Existing compositor override /etc/environment.d/{name}; clear it before switching."
    return None


def clear_primary():
    for path, owned in reversed(boot_mounts()):
        if owned:
            command("umount", "--", str(path))


def set_primary(selected: dict, devices: list[dict]):
    targets = [(SYS / "bus/pci/devices" / g["bdf"] / "boot_vga", g["bdf"] == selected["bdf"])
               for g in devices]
    if not all(path.exists() for path, _ in targets):
        raise RuntimeError("A GPU has no boot_vga attribute; this platform is not supported")
    clear_primary()
    try:
        RUN.mkdir(parents=True, exist_ok=True)
        for value in ("0", "1"):
            (RUN / value).write_text(value + "\n")
        for path, primary in targets:
            command("mount", "--bind", "-o", "ro", str(RUN / str(int(primary))), str(path))
        if not all(read(path) == str(int(primary)) for path, primary in targets):
            raise RuntimeError("GPU selection did not read back correctly")
    except Exception:
        clear_primary()
        raise


def busy() -> bool:
    return command("systemctl", "is-active", UNIT, check=False) in {"active", "activating", "deactivating"}


def gpu_model(gpu: dict) -> str:
    path = SYS / "bus/pci/devices" / gpu["bdf"]
    product = read(path / "product_name")
    if product:
        return product
    # Navi 32's PCI device ID is shared by desktop and mobile products.
    # libdrm's revision-specific names distinguish RX 7800M from RX 7800 XT.
    try:
        identity = (int(gpu["device"], 16), int(read(path / "revision"), 16))
    except ValueError:
        identity = None
    if gpu["vendor"] == "0x1002" and identity:
        for database in AMD_IDS:
            for line in read(database).splitlines():
                fields = line.split("#", 1)[0].split(",", 2)
                if len(fields) != 3:
                    continue
                try:
                    key = (int(fields[0].strip(), 16), int(fields[1].strip(), 16))
                except ValueError:
                    continue
                if key == identity and fields[2].strip():
                    return fields[2].strip()
    try:
        output = command("lspci", "-D", "-s", gpu["bdf"], "-vmm", timeout=3)
        for line in output.splitlines():
            if line.startswith("Device:"):
                name = line.split(":", 1)[1].strip()
                if name:
                    return name
    except (OSError, RuntimeError):
        pass
    return f"AMD GPU ({gpu['vendor'].removeprefix('0x')}:{gpu['device'].removeprefix('0x')})"


def status() -> dict:
    error = None
    try:
        gpu = choose_egpu(gpus())
    except RuntimeError as exc:
        gpu, error = None, str(exc)
    power = None
    if gpu:
        path = SYS / "bus/pci/devices" / gpu["bdf"]
        for cap in (path / "hwmon").glob("hwmon*/power1_cap"):
            try:
                power = int(read(cap)) / 1_000_000
            except ValueError:
                pass
    last = load(RUN / "result.json")
    return {"connected": gpu is not None, "selected": bool(gpu and gpu["selected"]),
            "gpu_name": gpu_model(gpu) if gpu else None,
            "bus_id": gpu["bdf"] if gpu else None, "power_cap_w": power,
            "busy": busy(), "error": error or last.get("error"),
            "message": last.get("message"), "conflict": conflict()}


def wait_for_gpu() -> dict:
    gpu = choose_egpu(gpus())
    if not gpu or not gpu["ready"]:
        write_sysfs(SYS / "bus/pci/rescan", "1")
    for _ in range(20):
        gpu = choose_egpu(gpus())
        if gpu and gpu["ready"]:
            return gpu
        time.sleep(1)
    raise RuntimeError("No ready AMD eGPU found. Check power/cable and USB4 authorization; replug if necessary.")


def eject_functions(gpu: dict) -> list[Path]:
    slot = gpu["bdf"].rsplit(".", 1)[0]
    paths = sorted((SYS / "bus/pci/devices").glob(slot + ".*"), reverse=True)
    # Remove only display and HDMI audio functions, never the enclosure's
    # USB controller, NVMe drive, upstream bridges, or a shared AMD module.
    if any(read(p / "class")[:4] != "0x03" and read(p / "class") != "0x040300" for p in paths):
        raise RuntimeError("Unexpected GPU sibling function; automatic eject is not supported")
    return paths


def running_steam_games() -> list[dict]:
    """Steam games currently running, found via the SteamAppId environment
    variable Steam sets on every game process. AppId 0 is the client itself."""
    games: dict[int, str] = {}
    for proc in sorted(PROC.glob("[0-9]*"), key=lambda p: int(p.name)):
        try:
            env = (proc / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        appid = None
        for entry in env:
            if entry.startswith(b"SteamAppId="):
                try:
                    appid = int(entry.split(b"=", 1)[1])
                except ValueError:
                    appid = None
                break
        if not appid or appid in games:
            continue
        try:
            games[appid] = (proc / "comm").read_text().strip()
        except OSError:
            games[appid] = f"app {appid}"
    return [{"appid": appid, "name": games[appid]} for appid in sorted(games)]


def snapshot_games_for_relaunch() -> list[dict]:
    games = running_steam_games()
    save(RUN / "pending_relaunch.json", {"games": games})
    return games


def load_pending_games() -> list[dict]:
    data = load(RUN / "pending_relaunch.json")
    games = data.get("games")
    return [g for g in games if isinstance(g, dict) and g.get("appid")] if isinstance(games, list) else []


def clear_pending_games():
    save(RUN / "pending_relaunch.json", {"games": []})


def steam_running(uid: int) -> bool:
    return bool(command("pgrep", "-u", str(uid), "-x", "steam", check=False))


def relaunch_pending_games(uid: int, wait_seconds: int = 120) -> str:
    """Relaunch games snapshotted before eject, once the Steam client is back.
    Only reopens the game; in-game progress is whatever was saved to disk."""
    games = load_pending_games()
    if not games:
        return "No game to reopen."
    for _ in range(wait_seconds // 2):
        if steam_running(uid):
            break
        time.sleep(2)
    else:
        raise RuntimeError("Steam client did not come back after resume; game left closed.")
    try:
        user = pwd.getpwuid(uid)
    except KeyError:
        raise RuntimeError(f"Cannot resolve username for uid {uid}")
    launched, failed = [], []
    for game in games:
        try:
            appid = int(game["appid"])
        except (TypeError, ValueError):
            failed.append(f"{game.get('name') or '?'} (bad app id)")
            continue
        try:
            command("runuser", "-u", user.pw_name, "--", "env",
                    f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus",
                    f"XDG_RUNTIME_DIR=/run/user/{uid}", f"HOME={user.pw_dir}",
                    "/usr/bin/steam", f"steam://rungameid/{appid}", timeout=30)
            launched.append(game.get("name") or str(appid))
        except RuntimeError as exc:
            failed.append(f"{game.get('name') or appid} ({exc})")
        time.sleep(3)
    clear_pending_games()
    if failed and not launched:
        raise RuntimeError("Could not reopen: " + "; ".join(failed))
    message = "Reopened " + ", ".join(launched) + " after resume. Progress is only what was saved in-game."
    if failed:
        message += " Failed: " + "; ".join(failed)
    save(RUN / "result.json", {"message": message})
    return message


def suspend_and_wait():
    # `systemctl suspend` only queues the request. Keep the DM stopped until
    # the clocks prove that suspend actually occurred and the machine woke.
    offset = time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()
    command("systemctl", "suspend")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic() - offset > 0.1:
            # The kernel is awake before systemd-sleep thaws user.slice.
            # Starting user@UID in that window fails with "frozen unit".
            for _ in range(60):
                freezer = command("systemctl", "show", "user.slice", "--property=FreezerState", "--value")
                sleep_state = command("systemctl", "is-active", "systemd-suspend.service", check=False)
                if freezer == "running" and sleep_state not in {"active", "activating", "deactivating"}:
                    return
                time.sleep(0.25)
            raise RuntimeError("User sessions did not thaw after resume")
        time.sleep(0.25)
    raise RuntimeError("Suspend did not occur within 30 seconds; GPU remains ejected")


def start_session(uid: int):
    for attempt in range(30):
        try:
            command("systemctl", "start", f"user@{uid}.service", "display-manager.service", timeout=45)
            return
        except RuntimeError as exc:
            if "frozen unit" not in str(exc) or attempt == 29:
                raise
            time.sleep(0.5)


def operate(action: str, uid: int):
    if action not in ACTIONS or uid < 1000:
        raise RuntimeError("Invalid operation or user ID")
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN / "operation.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("An operation is already running")
        save(RUN / "result.json", {"message": f"Running {action}…"})
        stopped = False
        try:
            if action not in {"restart", "restore"}:
                problem = conflict()
                if problem:
                    raise RuntimeError(problem)
            devices = gpus()
            gpu = None
            functions = []
            if action == "switch":
                gpu = wait_for_gpu()
                devices = gpus()
            elif action in {"eject", "eject-sleep"}:
                gpu = choose_egpu(devices)
                if not gpu:
                    raise RuntimeError("No eGPU connected")
                internal = choose_internal(devices)
                functions = eject_functions(gpu)
            # Set before stopping so the finally block recovers partial stops.
            stopped = True
            command("systemctl", "stop", "display-manager.service", f"user@{uid}.service", timeout=45)
            if action == "switch":
                set_primary(gpu, devices)
            elif action in {"eject", "eject-sleep"}:
                set_primary(internal, devices)
                # Unmount the eGPU's flag before removing its sysfs node.
                for path, owned in boot_mounts():
                    if owned and gpu["bdf"] in str(path):
                        command("umount", "--", str(path))
                for path in functions:
                    if (path / "driver").exists():
                        write_sysfs(path / "driver/unbind", path.name)
                    write_sysfs(path / "remove", "1")
                if any(path.exists() for path in functions):
                    raise RuntimeError("GPU is still enumerated; eject was not completed")
                if action == "eject-sleep":
                    suspend_and_wait()
            elif action == "restore":
                clear_primary()
        except Exception as exc:
            save(RUN / "result.json", {"error": str(exc)})
            raise
        finally:
            if stopped:
                try:
                    command("systemctl", "reset-failed", "display-manager.service", check=False)
                    start_session(uid)
                except Exception as exc:
                    previous = load(RUN / "result.json").get("error", "")
                    save(RUN / "result.json", {"error": f"{previous} Display recovery failed: {exc}".strip()})
                    raise
        messages = {"switch": "eGPU selected; graphical session restarted.",
                    "eject": "GPU ejected. Enclosure storage/USB remain attached; unmount storage before unplugging.",
                    "eject-sleep": "GPU ejected; resumed from sleep.",
                    "restart": "Display manager restarted.", "restore": "Original GPU selection restored."}
        save(RUN / "result.json", {"message": messages[action]})


def launch(action: str, uid: int):
    if action not in ACTIONS or not isinstance(uid, int) or uid < 1000:
        raise RuntimeError("Invalid operation")
    if busy():
        raise RuntimeError("An operation is already running")
    command("systemctl", "reset-failed", UNIT, check=False)
    command("systemd-run", "--unit=" + UNIT, "--collect", "--service-type=exec",
            "--property=RuntimeMaxSec=180", "--property=TimeoutStopSec=5",
            f"--property=ExecStopPost=/usr/bin/systemctl start user@{uid}.service display-manager.service",
            "/usr/bin/python3", str(Path(__file__).resolve()), action, "--uid", str(uid))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=sorted(ACTIONS | {"status"}))
    parser.add_argument("--uid", type=int, default=1000)
    parser.add_argument("--launch", action="store_true", help="Run in a resilient systemd worker")
    args = parser.parse_args()
    if args.action == "status":
        print(json.dumps(status(), indent=2))
    elif args.launch:
        launch(args.action, args.uid)
    else:
        operate(args.action, args.uid)
