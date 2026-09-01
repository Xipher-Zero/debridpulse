from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def text(path):
    return (ROOT / path).read_text()






def test_sqlite_is_the_only_runtime_database():
    production = [
        "backend/db/database.py", "backend/core/config.py", "backend/main.py",
        "backend/api/routes.py", "backend/services/db_maintenance.py",
    ]
    combined = "\n".join(text(path).lower() for path in production)
    assert "asyncpg" not in combined
    assert "postgresql" not in combined
    assert "db_type" not in text("backend/core/config.py")
    assert not (ROOT / "backend/db/migration.py").exists()




def test_security_contracts():
    routes = text("backend/api/routes.py")
    main = text("backend/main.py")
    policy = text("backend/auth/policy.py")
    config = text("backend/core/config.py")
    assert "_SECRET_SETTINGS" in routes
    assert 'data[field] = ""' in routes
    assert '"/api/health"' in policy
    assert '"/api/stats"' not in policy
    assert "enforce_authentication" in main
    assert "enforce_general_web_security" in main
    assert 'allow_origins=["*"]' not in main
    assert "atomic_write_json(CONFIG_PATH, data, indent=2)" in config
    secure_files = text("backend/core/secure_files.py")
    assert "tempfile.mkstemp" in secure_files
    assert "os.fchmod(fd, 0o600)" in secure_files




def test_removed_runtime_scope_is_not_exposed_in_frontend():
    js = text("frontend/static/app.js")
    for stale in ("PostgreSQL (external)", "s-postgres_host", "btn-test-postgres", "symlink-settings"):
        assert stale not in js


def test_fastapi_lifespan_has_single_context_manager_boundary():
    main = text("backend/main.py")
    assert "@asynccontextmanager\n@asynccontextmanager\nasync def lifespan" not in main
    assert main.count("@asynccontextmanager\nasync def lifespan(app: FastAPI):") == 1
    assert "_PG_CONNECT_RETRIES" not in main
    assert "_PG_CONNECT_DELAY" not in main
