"""Create a Decky-compatible ZIP without development dependencies."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

root = Path(__file__).resolve().parents[1]
output = root / "out"
output.mkdir(exist_ok=True)
with ZipFile(output / "decky-onexgpu.zip", "w", ZIP_DEFLATED) as archive:
    for name in ("package.json", "plugin.json", "main.py", "helper.py", "README.md", "LICENSE", "dist/index.js"):
        archive.write(root / name, f"decky-onexgpu/{name}")
print(output / "decky-onexgpu.zip")
