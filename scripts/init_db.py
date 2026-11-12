import os
import sys
import asyncio
from pathlib import Path

# Ensure project root is in sys.path
ROOT_DIR = Path(__file__).parent.parent.resolve()
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import asyncpg
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

from services.main_app.models import Base as MainBase
from services.auth_service.models import Base as AuthBase

MAIN_DB_NAME = os.getenv("MAIN_DB_NAME", "main_app_db")
AUTH_DB_NAME = os.getenv("AUTH_DB_NAME", "auth_service_db")
PG_USER = os.getenv("POSTGRES_USER", "postgres")
PG_PASS = os.getenv("POSTGRES_PASSWORD", "postgres")
PG_HOST = os.getenv("POSTGRES_HOST", "localhost")
PG_PORT = int(os.getenv("POSTGRES_PORT", "5432"))

MAIN_DATABASE_URL = f"postgresql+asyncpg://{PG_USER}:{PG_PASS}@{PG_HOST}:{PG_PORT}/{MAIN_DB_NAME}"
AUTH_DATABASE_URL = f"postgresql+asyncpg://{PG_USER}:{PG_PASS}@{PG_HOST}:{PG_PORT}/{AUTH_DB_NAME}"

async def create_database_if_not_exists(db_name: str):
    try:
        conn = await asyncpg.connect(user=PG_USER, password=PG_PASS, host=PG_HOST, port=PG_PORT, database="postgres")
        db_exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db_name)
        if not db_exists:
            print(f"Creating database '{db_name}'...")
            await conn.execute(f'CREATE DATABASE "{db_name}"')
            print(f"Database '{db_name}' created successfully.")
        else:
            print(f"Database '{db_name}' already exists.")
        await conn.close()
    except Exception as e:
        print(f"Notice/Error checking database {db_name}: {e}")

async def init_tables():
    await create_database_if_not_exists(MAIN_DB_NAME)
    await create_database_if_not_exists(AUTH_DB_NAME)

    # Initialize main_app_db tables
    engine_main = create_async_engine(MAIN_DATABASE_URL, echo=False)
    async with engine_main.begin() as conn:
        await conn.run_sync(MainBase.metadata.create_all)
        try:
            await conn.execute(text("SELECT setval('comments_id_seq', COALESCE((SELECT MAX(id) FROM comments), 1))"))
        except Exception:
            pass
    await engine_main.dispose()
    print("Tables and sequences for main_app_db initialized successfully.")

    # Initialize auth_service_db tables
    engine_auth = create_async_engine(AUTH_DATABASE_URL, echo=False)
    async with engine_auth.begin() as conn:
        await conn.run_sync(AuthBase.metadata.create_all)
    await engine_auth.dispose()
    print("Tables for auth_service_db initialized successfully.")

if __name__ == "__main__":
    asyncio.run(init_tables())
