#!/usr/bin/env python
"""
updater.py — secure, signed application updates for the Windows build.

Two roles, both keyed by the owner's RSA-2048 keypair (public key embedded via
licensing.py, private key only on the owner's machine):

  OWNER  (run directly, e.g. `python updater.py build <version> <dry-dir> <stage-dir>`)
         Produces a ready-to-upload bundle folder: the listed files plus a
         signed `update.json` manifest (RSA signature over a canonical list of
         {path,size,sha256}). Only the owner can create a valid manifest, so a
         target machine will refuse any not-owned bundle.

  CLIENT (imported by app.py)
         fetch_manifest(url)  -> fetch + verify signature
         check_for_update     -> compare advertised version against local
         download_bundle      -> pull every file to a stage dir, verifying sha256

update.json layout:
    {
      "version": "1.1.0",
      "schema": 1,
      "files": [ {"path": "LeiturgiaServer.exe", "size": 123, "sha256": "..."}, ... ],
      "signature": "<hex RSA signature over canonical string>"
    }

Canonical string (both sides must build identically):
    "<version>\\n" + for each file sorted by path: "<path>\\t<size>\\t<sha256>\\n"
The files list must be sorted by path.
"""
import hashlib
import json
import os

from licensing import rsa_sign, rsa_verify

MANIFEST_NAME = "update.json"
SCHEMA = 1

# Paths relative to an app install folder that hold per-machine / user data and
# MUST be carried across an update (never overwritten by the bundle).
PRESERVE = frozenset({"license.dat", "config.json"})
PRESERVE_DIRS = ("data", "media", "output")


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def sha256_file(path, chunk=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def hash_dir(path, base):
    """Yield (relpath, size, sha256) for every file under path, paths '\'-joined
    and relative to base. Returns a sorted list."""
    out = []
    for root, _dirs, files in os.walk(path):
        for name in sorted(files):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, base).replace("/", "\\")
            out.append((rel, os.path.getsize(full), sha256_file(full)))
    return sorted(out, key=lambda x: x[0].lower())


def canonical(files, version):
    lines = [str(version)]
    for rel, size, sha in files:
        lines.append("%s\t%d\t%s" % (rel, size, sha))
    return "\n".join(lines)


def sign(files, version):
    """Owner side: RSA-sign the canonical manifest string (needs private key)."""
    return rsa_sign(canonical(files, version).encode())


def verify(files, version, signature):
    """Client side: verify the manifest signature with the embedded public key."""
    return rsa_verify(canonical(files, version).encode(), signature)


# --------------------------------------------------------------------------- #
# OWNER: build an update bundle
# --------------------------------------------------------------------------- #
def build_bundle(version, dry_dir, stage_dir, manifest_name=MANIFEST_NAME):
    """Copy the distributable files from dry_dir into stage_dir and write a
    signed update.json manifest. Returns the manifest dict."""
    os.makedirs(stage_dir, exist_ok=True)

    # Which top-level source files/folders become part of the bundle.
    include_files = ("Leiturgia.exe", "LeiturgiaServer.exe", "config.json", "app.version")
    include_dirs = ("data", "media", "output")

    staging = os.path.join(stage_dir, "__src")
    os.makedirs(staging, exist_ok=True)
    for name in include_files:
        if os.path.exists(os.path.join(dry_dir, name)):
            _copy_tree(os.path.join(dry_dir, name), os.path.join(staging, name))
    for d in include_dirs:
        src = os.path.join(dry_dir, d)
        if os.path.isdir(src) and os.listdir(src):
            _copy_tree(src, os.path.join(staging, d))

    files = hash_dir(staging, staging)
    files = [(p, s, h) for (p, s, h) in files
             if os.path.normpath(p).lower() != os.path.normpath(manifest_name).lower()]

    manifest = {
        "version": version,
        "schema": SCHEMA,
        "files": [{"path": p, "size": s, "sha256": h} for (p, s, h) in files],
        "signature": sign(files, version),
    }
    with open(os.path.join(staging, manifest_name), "w") as f:
        json.dump(manifest, f, indent=2)

    # Move __src contents into stage_dir (the uploadable folder).
    for name in os.listdir(staging):
        _copy_tree(os.path.join(staging, name), os.path.join(stage_dir, name))
    _rmtree(staging)
    return manifest


def _copy_tree(src, dst):
    if os.path.isdir(src):
        for root, _dirs, files in os.walk(src):
            rel = os.path.relpath(root, src)
            target = dst if rel == "." else os.path.join(dst, rel)
            os.makedirs(target, exist_ok=True)
            for name in files:
                with open(os.path.join(root, name), "rb") as f:
                    data = f.read()
                with open(os.path.join(target, name), "wb") as g:
                    g.write(data)
    else:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(src, "rb") as f:
            data = f.read()
        with open(dst, "wb") as g:
            g.write(data)


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------- #
# CLIENT: fetch + verify + download
# --------------------------------------------------------------------------- #
def _http_get(url, timeout=60):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "LeiturgiaUpdater/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_manifest(base_url, timeout=30):
    """Download and verify update.json from base_url. Returns the manifest or
    raises ValueError/RuntimeError."""
    raw = _http_get(_join(base_url, MANIFEST_NAME), timeout=timeout)
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise ValueError("update.json is not valid JSON: %s" % e)

    version = manifest.get("version")
    files = manifest.get("files")
    signature = manifest.get("signature")
    if not version or not isinstance(files, list) or not signature:
        raise ValueError("update.json is malformed (missing version/files/signature)")

    norm = [(f.get("path"), int(f.get("size", 0)), f.get("sha256")) for f in files]
    norm = [(p, s, h) for (p, s, h) in norm if p]
    if not verify(norm, version, signature):
        raise ValueError("update signature verification FAILED — refusing update")
    return manifest


def _join(base_url, rel):
    # GitHub release assets are always served FLAT from the release root
    # (e.g. .../download/v1.4.1/update.json). The manifest keeps the on-disk
    # layout in each file path (e.g. data\bible_en.db), so downloads must fetch
    # each asset by its basename — the subdirectory-shaped URL would 404.
    name = rel.replace("\\", "/").split("/")[-1]
    return base_url.rstrip("/") + "/" + name


def version_tuple(v):
    """Parse '1.2.3'-style version to a comparable tuple; unknown -> lowest."""
    if not v:
        return (0, 0, 0)
    parts = []
    for part in str(v).split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def check_for_update(base_url, current_version, timeout=30):
    """Return a dict describing the update, either {'available': False} or
    {'available': True, 'latest', 'manifest'}."""
    manifest = fetch_manifest(base_url, timeout=timeout)
    latest = manifest["version"]
    if version_tuple(latest) <= version_tuple(current_version):
        return {"available": False, "latest": latest, "current": current_version}
    return {"available": True, "latest": latest, "current": current_version, "manifest": manifest}


def download_bundle(manifest, stage_dir, base_url, on_file=None, timeout=60):
    """Download every file in the manifest to stage_dir, verifying each sha256."""
    os.makedirs(stage_dir, exist_ok=True)
    total = len(manifest["files"])
    for i, f in enumerate(manifest["files"], 1):
        rel = f["path"]
        parts = rel.split("/") if "/" in rel else rel.split("\\")
        dest = os.path.join(stage_dir, *parts)
        dest_dir = os.path.dirname(dest)
        os.makedirs(dest_dir, exist_ok=True)
        data = _http_get(_join(base_url, rel), timeout=timeout)
        if len(data) != f["size"]:
            raise ValueError("size mismatch for %s (got %d, expected %d)" % (rel, len(data), f["size"]))
        if hashlib.sha256(data).hexdigest() != f["sha256"]:
            raise ValueError("sha256 mismatch for %s — file corrupted" % rel)
        with open(dest, "wb") as g:
            g.write(data)
        if on_file:
            on_file(i, total, rel)
    return True


# --------------------------------------------------------------------------- #
# CLI entry points
# --------------------------------------------------------------------------- #
def _cli():
    import sys
    args = sys.argv[1:]
    if len(args) >= 3 and args[0] == "build":
        version, dry_dir, stage_dir = args[1], args[2], args[3]
        manifest = build_bundle(version, dry_dir, stage_dir)
        print("Built update bundle version %s -> %s" % (version, stage_dir))
        print("Files: %d" % len(manifest["files"]))
        print("Signature: %s" % manifest["signature"][:24] + "...")
    else:
        print("usage: updater.py build <version> <dry-dir> <stage-dir>")
        sys.exit(2)


if __name__ == "__main__":
    _cli()
