"""Build the distributable zip: dist/IponMirror-<version>-win64.zip

  1. engine/  : uxplay.exe + only the GStreamer plugins it needs + their DLLs (from MSYS2 UCRT64)
  2. app      : IponMirror.exe built with PyInstaller (onedir)
  3. licenses : GPL-3.0 text, UxPlay patch/source info, license files of every bundled MSYS2 package

usage:  py -3.11 scripts/package.py            (after scripts/build_engine.ps1)
        py -3.11 scripts/package.py --engine-only
"""
import os
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MSYS = r"C:\msys64"
UCRT = os.path.join(MSYS, "ucrt64")
UCRT_BIN = os.path.join(UCRT, "bin")
OBJDUMP = os.path.join(UCRT_BIN, "objdump.exe")
ENGINE_EXE = os.path.join(ROOT, "build", "UxPlay", "build", "uxplay.exe")
DIST = os.path.join(ROOT, "dist")
OUT = os.path.join(DIST, "IponMirror")
ENGINE_OUT = os.path.join(OUT, "engine")

sys.path.insert(0, os.path.join(ROOT, "app"))
from ipon_mirror import VERSION  # noqa: E402

# GStreamer plugins used by UxPlay's pipelines (video: appsrc ! h264parse ! decoder ! convert ! sink,
# audio: appsrc ! avdec_aac/alac ! audioconvert ! audioresample ! volume ! sink)
PLUGINS = [
    "coreelements", "app", "typefindfunctions", "playback", "autodetect",
    "videoparsersbad", "d3d12", "d3d11", "libav", "videoconvertscale", "videorate",
    "audioconvert", "audioresample", "volume", "level", "audioparsers", "wasapi", "wasapi2", "directsound",
    "videofilter", "jpeg", "imagefreeze",
]


def run(cmd, **kw):
    env = dict(os.environ, LANG="C", LC_ALL="C")  # English pacman output
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, **kw)


def dll_deps(path):
    out = run([OBJDUMP, "-p", path]).stdout
    return re.findall(r"DLL Name:\s*(\S+)", out)


def copy_with_deps(files, dll_dir):
    """copy files, then every DLL they need that MSYS2 provides, into dll_dir (recursively)"""
    copied = set()
    todo = list(files)
    while todo:
        src, dst_dir = todo.pop()
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, os.path.basename(src))
        if os.path.normcase(dst) in copied:
            continue
        shutil.copy2(src, dst)
        copied.add(os.path.normcase(dst))
        for name in dll_deps(src):
            cand = os.path.join(UCRT_BIN, name)
            if os.path.exists(cand) and os.path.normcase(os.path.join(dll_dir, name)) not in copied:
                todo.append((cand, dll_dir))
    return copied


def owning_packages(paths):
    pkgs = set()
    bash = os.path.join(MSYS, "usr", "bin", "bash.exe")
    for p in paths:
        rel = os.path.basename(p)
        for base in ("bin", "lib/gstreamer-1.0", "libexec/gstreamer-1.0"):
            src = os.path.join(UCRT, base, rel)
            if os.path.exists(src):
                r = run([bash, "-lc", f"pacman -Qqo /ucrt64/{base}/{rel}"])
                if r.returncode == 0:
                    pkgs.update(r.stdout.split())
                break
    return sorted(pkgs)


def package_engine():
    if not os.path.exists(ENGINE_EXE):
        sys.exit("engine not built: run scripts/build_engine.ps1 first")
    shutil.rmtree(ENGINE_OUT, ignore_errors=True)
    plugin_dir = os.path.join(ENGINE_OUT, "lib", "gstreamer-1.0")
    files = [(ENGINE_EXE, ENGINE_OUT),
             (os.path.join(UCRT, "libexec", "gstreamer-1.0", "gst-plugin-scanner.exe"),
              os.path.join(ENGINE_OUT, "libexec"))]
    for name in PLUGINS:
        p = os.path.join(UCRT, "lib", "gstreamer-1.0", f"libgst{name}.dll")
        if not os.path.exists(p):
            sys.exit(f"missing plugin: {p}")
        files.append((p, plugin_dir))
    copied = copy_with_deps(files, ENGINE_OUT)
    print(f"engine: {len(copied)} files")

    # licenses of bundled MSYS2 packages
    lic_dir = os.path.join(OUT, "licenses")
    shutil.rmtree(lic_dir, ignore_errors=True)
    os.makedirs(lic_dir)
    pkgs = owning_packages(copied)
    bash = os.path.join(MSYS, "usr", "bin", "bash.exe")
    info = run([bash, "-lc", "pacman -Qi " + " ".join(pkgs)]).stdout
    lines = ["Ipon Mirror bundles the following MSYS2 packages (binaries, unmodified).",
             "Source code: https://github.com/msys2/MINGW-packages and each project's URL below.", ""]
    for block in info.strip().split("\n\n"):
        fields = dict(re.findall(r"^(\S[^:]*?)\s*:\s*(.*)$", block, re.M))
        lines.append(f"{fields.get('Name', '?')} {fields.get('Version', '')}")
        for k in ("Licenses", "URL"):
            if k in fields:
                lines.append(f"    {k}: {fields[k]}")
    with open(os.path.join(lic_dir, "THIRD_PARTY.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    for pkg in pkgs:
        name = pkg.replace("mingw-w64-ucrt-x86_64-", "")
        src = os.path.join(UCRT, "share", "licenses", name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(lic_dir, name), dirs_exist_ok=True)
    shutil.copy2(os.path.join(ROOT, "build", "UxPlay", "LICENSE"), os.path.join(lic_dir, "UxPlay-LICENSE.txt"))
    shutil.copy2(os.path.join(ROOT, "engine", "uxplay-ipon.patch"), os.path.join(lic_dir, "uxplay-ipon.patch"))
    print(f"licenses: {len(pkgs)} packages")


def package_app():
    work = os.path.join(ROOT, "build", "pyinstaller")
    app = os.path.join(ROOT, "app")
    r = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
                        "--name", "IponMirror", "--icon", os.path.join(app, "assets", "icon.ico"),
                        "--add-data", f"{os.path.join(app, 'assets')}{os.pathsep}assets",
                        "--distpath", os.path.join(work, "dist"), "--workpath", os.path.join(work, "work"),
                        "--specpath", work, os.path.join(app, "ipon_mirror.py")])
    if r.returncode:
        sys.exit("PyInstaller failed")
    built = os.path.join(work, "dist", "IponMirror")
    for name in os.listdir(built):
        s, d = os.path.join(built, name), os.path.join(OUT, name)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)
    shutil.copy2(os.path.join(ROOT, "LICENSE"), os.path.join(OUT, "LICENSE.txt"))
    shutil.copy2(os.path.join(ROOT, "docs", "README-zip.txt"), os.path.join(OUT, "README.txt"))


def make_zip():
    path = os.path.join(DIST, f"IponMirror-{VERSION}-win64.zip")
    if os.path.exists(path):
        os.remove(path)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for d, _, fs in os.walk(OUT):
            for f in fs:
                full = os.path.join(d, f)
                z.write(full, os.path.relpath(full, DIST))
    print(f"zip: {path} ({os.path.getsize(path) / 1e6:.1f} MB)")


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    package_engine()
    if "--engine-only" not in sys.argv:
        package_app()
        make_zip()
