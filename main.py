import subprocess
import sys
import time

def main():
    print("Starting Auth Service (Brain) on port 8001...")
    auth_service = subprocess.Popen([sys.executable, "-m", "uvicorn", "auth_service:app", "--host", "0.0.0.0", "--port", "8001"])
    
    print("Starting Main App (Resource) on port 8000...")
    main_app = subprocess.Popen([sys.executable, "-m", "uvicorn", "main_app:app", "--host", "0.0.0.0", "--port", "8000"])
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping services...")
        auth_service.terminate()
        main_app.terminate()
        auth_service.wait()
        main_app.wait()
        print("Services stopped.")

if __name__ == "__main__":
    main()
