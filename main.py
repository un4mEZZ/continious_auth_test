"""
Unified Runner for Continuous Authentication Microservices System.
Usage:
    python main.py                # Run both Auth Service and Main App
    python main.py --reset-auth   # Clear auth events/profiles before starting
    python main.py --auth-only    # Run only Auth Service
    python main.py --app-only     # Run only Main App
"""
import os
import sys
import time
import argparse
import asyncio
import subprocess
from pathlib import Path

# Add project root to sys.path
ROOT_DIR = Path(__file__).parent.resolve()
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

async def clear_auth_data():
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text

    db_url = os.getenv(
        "AUTH_DATABASE_URL", 
        "postgresql+asyncpg://postgres:postgres@localhost:5432/auth_service_db"
    )
    print("=====================================================")
    print("Clearing behavioral profiles and authentication events...")
    engine = create_async_engine(db_url, echo=False)
    
    try:
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE TABLE auth_events RESTART IDENTITY;"))
            reset_json = '{"status": "learning", "events_collected": 0}'
            await conn.execute(text(f"UPDATE behavior_profiles SET features = '{reset_json}'::jsonb;"))
        print("Auth data cleared successfully. Profiles reset to 'learning' mode.")
    except Exception as e:
        print(f"Notice: Could not reset auth data: {e}")
    finally:
        await engine.dispose()
    print("=====================================================\n")

def main():
    parser = argparse.ArgumentParser(description="Start Continuous Auth System services.")
    parser.add_argument("--reset-auth", action="store_true", help="Clear auth data before starting services")
    parser.add_argument("--auth-only", action="store_true", help="Start only Auth Service")
    parser.add_argument("--app-only", action="store_true", help="Start only Main Web App")
    args = parser.parse_args()

    if args.reset_auth:
        asyncio.run(clear_auth_data())

    processes = []

    try:
        if not args.app_only:
            print("Starting Auth Service (FastAPI :8001 + gRPC :50051)...")
            auth_proc = subprocess.Popen([
                sys.executable, "-m", "uvicorn", 
                "services.auth_service.app:app", 
                "--host", "0.0.0.0", 
                "--port", "8001"
            ])
            processes.append(auth_proc)
            # Give Auth Service time to initialize gRPC server
            time.sleep(2)

        if not args.auth_only:
            print("Starting Main App (FastAPI :8000)...")
            main_proc = subprocess.Popen([
                sys.executable, "-m", "uvicorn", 
                "services.main_app.app:app", 
                "--host", "0.0.0.0", 
                "--port", "8000"
            ])
            processes.append(main_proc)

        print("\nAll requested services started. Press Ctrl+C to terminate.")
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print("\nStopping services gracefully...")
        for p in processes:
            p.terminate()
        for p in processes:
            p.wait()
        print("All services stopped.")

if __name__ == "__main__":
    main()
