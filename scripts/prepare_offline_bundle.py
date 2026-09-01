from __future__ import annotations

import argparse
import hashlib
import lzma
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path


PYPI_REQUIREMENTS = (
    "setuptools>=68",
    "wheel>=0.43",
    "packaging>=20.0",
    "requests>=2.31.0",
    "PyYAML>=6.0",
    "paramiko>=3.4.0",
    "matplotlib>=3.7.0",
    "fastapi>=0.115.0",
    "uvicorn>=0.30.0",
    "python-multipart>=0.0.9",
    "cryptography>=42.0.0",
    "python-docx>=1.1.0",
    "reportlab>=4.1.0",
    "pypdf>=4.0.0",
    "PyMySQL>=1.1.0",
    "minio>=7.2.0",
    "redis>=5.0.0",
)
PACKAGES_URL = "https://deb.debian.org/debian/dists/trixie/main/binary-amd64/Packages.xz"
DEBIAN_ROOT = "https://deb.debian.org/debian"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as response, target.open("wb") as stream:
        shutil.copyfileobj(response, stream)


def package_record(packages_xz: Path, package_name: str) -> dict[str, str]:
    text = lzma.decompress(packages_xz.read_bytes()).decode("utf-8")
    for paragraph in text.split("\n\n"):
        values: dict[str, str] = {}
        for line in paragraph.splitlines():
            if ": " in line:
                key, value = line.split(": ", 1)
                values[key] = value
        if values.get("Package") == package_name:
            return values
    raise RuntimeError(f"Debian package not found: {package_name}")


def write_manifest(bundle: Path) -> None:
    artifacts = sorted(
        path for path in bundle.rglob("*") if path.is_file() and path.name != "SHA256SUMS"
    )
    lines = [f"{sha256(path)}  {path.relative_to(bundle).as_posix()}" for path in artifacts]
    (bundle / "SHA256SUMS").write_bytes(("\n".join(lines) + "\n").encode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare the verified Linux/amd64 offline image bundle.")
    parser.add_argument("--output", type=Path, default=Path("vendor"))
    args = parser.parse_args()
    bundle = args.output.resolve()
    wheels = bundle / "wheels"
    fonts = bundle / "fonts"
    wheels.mkdir(parents=True, exist_ok=True)
    fonts.mkdir(parents=True, exist_ok=True)

    requirements = bundle / "requirements-linux.txt"
    requirements.write_text("\n".join(PYPI_REQUIREMENTS) + "\n", encoding="utf-8")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--dest",
            str(wheels),
            "--only-binary=:all:",
            "--platform=manylinux2014_x86_64",
            "--python-version=312",
            "--implementation=cp",
            "--abi=cp312",
            "--requirement",
            str(requirements),
        ],
        check=True,
    )

    packages_xz = bundle / "Packages.xz"
    download(PACKAGES_URL, packages_xz)
    record = package_record(packages_xz, "fonts-noto-cjk")
    filename = record["Filename"]
    font_deb = fonts / "fonts-noto-cjk.deb"
    download(f"{DEBIAN_ROOT}/{filename}", font_deb)
    actual = sha256(font_deb)
    expected = record["SHA256"]
    if actual != expected:
        raise RuntimeError(f"Font package hash mismatch: expected {expected}, got {actual}")

    write_manifest(bundle)
    print(f"Prepared {bundle}")
    print(f"Wheels: {len(list(wheels.iterdir()))}")
    print(f"Font SHA256: {actual}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
