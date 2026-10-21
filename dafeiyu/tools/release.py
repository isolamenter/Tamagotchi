"""Build a source release with a per-file SHA-256 manifest. Databases and credentials are excluded."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

from dafeiyu import __version__

ROOT = Path(__file__).resolve().parents[2]


def build_release(destination: Path) -> dict:
    candidates = [
        p
        for p in (ROOT / "dafeiyu").rglob("*")
        if p.is_file() and p.suffix in {".py", ".sql", ".html", ".css", ".js", ".svg", ".png"}
    ]
    candidates += list((ROOT / "config").glob("*.toml"))
    candidates += list((ROOT / "assets" / "memes").glob("*.png"))
    candidates.append(ROOT / "requirements.txt")
    payload = {}
    for path in sorted(candidates):
        if path.is_symlink():
            raise ValueError("Release inputs must be regular project files")
        payload[str(path.relative_to(ROOT))] = path.read_bytes()
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
    )
    manifest = {
        "version": __version__,
        "base_commit": revision,
        "working_tree_dirty": dirty,
        "files": {
            name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in payload.items()
        },
    }
    payload["manifest.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as output, tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, data in payload.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = (ROOT / name).stat().st_mtime if name in manifest["files"] else 0
            archive.addfile(info, io.BytesIO(data))
    with tarfile.open(destination, "r:gz") as archive:
        for name, expected in manifest["files"].items():
            data = archive.extractfile(name).read()
            if (
                hashlib.sha256(data).hexdigest() != expected["sha256"]
                or (ROOT / name).read_bytes() != data
            ):
                raise RuntimeError("Release verification failed or source changed during packaging")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    manifest = build_release(args.output)
    print(
        json.dumps(
            {
                "version": __version__,
                "files": len(manifest["files"]),
                "working_tree_dirty": manifest["working_tree_dirty"],
                "archive": str(args.output),
                "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
