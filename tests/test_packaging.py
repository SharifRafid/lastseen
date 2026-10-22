from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from lastseen import __version__

ROOT = Path(__file__).resolve().parents[1]


def _need(name: str) -> Path:
    path = ROOT / name
    if not path.exists():
        pytest.skip(f"{name} is not part of this checkout")
    return path


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
def test_install_sh_parses():
    subprocess.run(["sh", "-n", str(_need("install.sh"))], check=True, timeout=30)


def test_docker_files_agree():
    dockerfile = _need("Dockerfile").read_text()
    compose = _need("docker-compose.yml").read_text()
    assert "LASTSEEN_DATA_DIR=/data" in dockerfile and "LASTSEEN_HOST=0.0.0.0" in dockerfile
    assert "USER lastseen" in dockerfile and "/api/health" in dockerfile
    assert '"8765:8765"' in compose and ":/data" in compose


def test_version_is_metadata():
    pyproject = _need("pyproject.toml").read_text()
    assert f'version = "{__version__}"' in pyproject or __version__ == "0.0.0"
