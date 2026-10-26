import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from changeguard.auth import Principal  # noqa: E402
from changeguard.config import Settings  # noqa: E402
from changeguard.db import Database  # noqa: E402
from changeguard.seed import SeedData, build_seed  # noqa: E402
from changeguard.service import ChangeGuardService  # noqa: E402

ANALYST = Principal("alice", "analyst")
REVIEWER = Principal("rita", "reviewer")
VIEWER = Principal("vic", "viewer")
ADMIN = Principal("ada", "admin")


def make_service(seed: SeedData | None = None, **settings_kw) -> ChangeGuardService:
    tmp = tempfile.mkdtemp(prefix="cg_test_")
    db = Database(f"sqlite:///{tmp}/test.db")
    s = Settings(**settings_kw)
    s.retry_backoff_seconds = 0.0
    svc = ChangeGuardService(db, s)
    svc.init_database(seed or build_seed())
    return svc


def submit(svc, component="CMP-104", market="EU", description="Supplier end-of-life notice received", key=None,
           constraints=None, change_type="discontinued"):
    body = {"component_id": component, "change_type": change_type, "description": description, "target_market": market}
    if constraints:
        body["constraints"] = constraints
    return svc.submit_change(ANALYST, body, key or f"k-{component}-{market}-{abs(hash(description))}")
