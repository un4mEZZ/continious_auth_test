import sys
from pathlib import Path

# Add proto directory to sys.path so generated pb2_grpc can find pb2 directly
_proto_dir = str(Path(__file__).parent.resolve())
if _proto_dir not in sys.path:
    sys.path.insert(0, _proto_dir)

from . import auth_pb2
from . import auth_pb2_grpc

__all__ = ["auth_pb2", "auth_pb2_grpc"]
