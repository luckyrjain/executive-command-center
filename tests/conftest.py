import os

os.environ.setdefault("ECC_SESSION_SECRET", "test-secret-value-that-is-long-enough")
os.environ.setdefault("ECC_DATABASE_URL", "sqlite+pysqlite:///:memory:")
