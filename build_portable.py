"""Reproducible portable Windows package from official CPython distribution.

Requires MinGW-w64 compiler/windres, an extracted CPython tcltk.msi, CPython
embeddable ZIP and the two pinned runtime wheels. Does not install anything.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def build():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cc", default="x86_64-w64-mingw32-gcc")
    ap.add_argument("--windres", default="x86_64-w64-mingw32-windres")
    ap.add_argument("--python-embed", type=Path, required=True)
    ap.add_argument("--python-tcltk", type=Path, required=True)
    ap.add_argument("--tcl-license", type=Path, required=True)
    ap.add_argument("--wheels", type=Path, required=True)
    args = ap.parse_args()
    root = Path(__file__).resolve().parent
    target = root / "dist" / "CoinPokerConverter"
    if target.exists(): raise SystemExit("Destination exists. Choose a clean build directory; existing artifacts are not overwritten.")
    target.mkdir(parents=True)
    runtime = target / "runtime"
    runtime.mkdir()
    with zipfile.ZipFile(args.python_embed) as z:
        if z.testzip() is not None: raise ValueError("Corrupt CPython archive")
        z.extractall(runtime)
    for p in (args.python_tcltk / "DLLs").iterdir():
        if p.suffix in (".dll", ".pyd"): shutil.copy2(p, runtime / p.name)
    shutil.copytree(args.python_tcltk / "Lib" / "tkinter", runtime / "Lib" / "tkinter")
    shutil.copytree(args.python_tcltk / "tcl", runtime / "tcl", ignore=shutil.ignore_patterns("*.lib", "demos"))
    shutil.copy2(args.tcl_license, runtime / "tcl" / "tcl8.6" / "license.terms")
    (runtime / "python312._pth").write_text("python312.zip\n.\nLib\nLib/site-packages\n..\nimport site\n", "utf-8")
    site = runtime / "Lib" / "site-packages"
    site.mkdir(parents=True)
    wheels = []
    for pattern in ["tkinterdnd2-0.7.0-*.whl", "tzdata-2026.6-*.whl"]:
        matches = list(args.wheels.glob(pattern))
        if len(matches) != 1: raise ValueError(f"Expected one wheel: {pattern}")
        wheel = matches[0]
        with zipfile.ZipFile(wheel) as z:
            if z.testzip() is not None: raise ValueError("Corrupt wheel")
            z.extractall(site)
        wheels.append({"name": wheel.name, "sha256": sha(wheel)})
    shutil.copytree(root / "app", target / "app", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ["main.py", "app.ico", "README_RU.txt", "WINDOWS_CHECK.txt", "THIRD_PARTY.txt"]:
        shutil.copy2(root / name, target / name)
    objects = root / "build_tools" / "launcher"
    objects.mkdir(parents=True, exist_ok=True)
    subprocess.run([args.windres, "launcher.rc", "-O", "coff", "-o", str(objects / "resources.o")], cwd=root, check=True)
    subprocess.run([args.cc, "launcher.c", str(objects / "resources.o"), "-o", str(target / "CoinPokerConverter.exe"),
                    "-municode", "-mwindows", "-O2", "-Wall", "-Wextra", "-Werror", "-static", "-s", "-Wl,--nxcompat,--dynamicbase", "-luser32", "-lkernel32"], cwd=root, check=True)
    evidence = {"build_kind": "MinGW-w64 native launcher + application-local CPython 3.12.10 AMD64", "native_windows_runtime": "NOT VERIFIED",
                "python_embed_sha256": sha(args.python_embed), "wheels": wheels,
                "files": {str(p.relative_to(target)).replace(os.sep, "/"): sha(p) for p in sorted(target.rglob("*")) if p.is_file()}}
    (target / "Package_manifest.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), "utf-8")
    print(target)


if __name__ == "__main__": build()
