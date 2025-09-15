#coding=utf-8

"""
Checkpoint utilities shared by TUI commands and PTY supervisor.
Provides helpers to snapshot external files and restore them.
"""

import os
import json
import glob
import shutil
import hashlib


def load_manifest(path):
    data = {"include": []}
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                j = json.load(f)
                if isinstance(j, dict):
                    data.update(j)
    except Exception:
        pass
    return data


def save_manifest(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def rel_target(abs_path, base):
    ap = os.path.abspath(abs_path)
    try:
        rel = os.path.relpath(ap, base)
        if rel.startswith(".."):  # outside base
            return os.path.join("abs", ap.lstrip(os.sep))
        return rel
    except Exception:
        return os.path.join("abs", ap.lstrip(os.sep))


def _sha256(path):
    try:
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def copy_in(files, files_dir, base):
    os.makedirs(files_dir, exist_ok=True)
    for src in files:
        rel = rel_target(src, base)
        dst = os.path.join(files_dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            shutil.copy2(src, dst)
        except Exception:
            pass


def write_files_index(files, files_dir, base):
    index = []
    for src in files:
        rel = rel_target(src, base)
        dst = os.path.join(files_dir, rel)
        index.append({
            "src": os.path.abspath(src),
            "rel": rel,
            "sha256": _sha256(dst),
        })
    try:
        with open(os.path.join(files_dir, "files_index.json"), "w") as f:
            json.dump(index, f, indent=2)
    except Exception:
        pass


def restore_files(files_dir):
    index_path = os.path.join(files_dir, "files_index.json")
    try:
        with open(index_path, "r") as f:
            index = json.load(f)
    except Exception:
        index = []
    restored = 0
    for ent in index:
        rel = ent.get("rel")
        src = os.path.join(files_dir, rel)
        dst = ent.get("src")
        if not (rel and dst and os.path.isfile(src)):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            shutil.copy2(src, dst)
            restored += 1
        except Exception:
            pass
    return restored

