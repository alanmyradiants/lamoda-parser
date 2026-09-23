import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest

PG_BIN = next((p for p in sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True)), None) if Path("/usr/lib/postgresql").exists() else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def pg_url():
    """Временный Postgres-кластер, если в системе есть бинарники. Иначе тест пропускается."""
    if os.environ.get("TEST_DATABASE_URL"):
        yield os.environ["TEST_DATABASE_URL"]
        return
    if PG_BIN is None:
        pytest.skip("нет Postgres")
    tmp = Path(tempfile.mkdtemp(prefix="lamoda_pg_"))
    data = tmp / "data"
    port = _free_port()
    as_pg = ["runuser", "-u", "postgres", "--"] if os.geteuid() == 0 else []
    if as_pg:
        shutil.chown(tmp, "postgres")
    try:
        subprocess.run([*as_pg, str(PG_BIN / "initdb"), "-D", str(data), "-U", "test", "--auth=trust"], check=True, capture_output=True)
        subprocess.run(
            [*as_pg, str(PG_BIN / "pg_ctl"), "-D", str(data), "-l", str(tmp / "log"), "-w", "start",
             "-o", f"-p {port} -k {tmp} -h 127.0.0.1"],
            check=True, capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, LookupError) as e:
        pytest.skip(f"Postgres не запустился: {e}")
    yield f"postgresql://test@127.0.0.1:{port}/postgres"
    subprocess.run([*as_pg, str(PG_BIN / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"], capture_output=True)
    shutil.rmtree(tmp, ignore_errors=True)
