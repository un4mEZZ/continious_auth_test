"""
Helper script to compile protobuf definitions.
Usage:
    python generate_proto.py
"""
import sys
import subprocess
from pathlib import Path

def main():
    proto_dir = Path(__file__).parent.resolve()
    proto_file = proto_dir / "auth.proto"
    
    print(f"Compiling {proto_file.name} in {proto_dir}...")
    cmd = [
        sys.executable, "-m", "grpc_tools.protoc",
        f"-I{proto_dir}",
        f"--python_out={proto_dir}",
        f"--grpc_python_out={proto_dir}",
        str(proto_file)
    ]
    result = subprocess.run(cmd)
    if result.returncode == 0:
        print("Protobuf compilation succeeded!")
    else:
        print("Protobuf compilation failed! Make sure grpcio-tools is installed.")

if __name__ == "__main__":
    main()
