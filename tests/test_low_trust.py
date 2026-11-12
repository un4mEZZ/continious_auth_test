import os
import sys
from pathlib import Path

# Ensure project root is in sys.path
ROOT_DIR = Path(__file__).parent.parent.resolve()
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import asyncio
import json
import time
import websockets
import grpc
from proto import auth_pb2, auth_pb2_grpc
from tests.test_low_score import generate_bot_events

AUTH_GRPC_URL = os.getenv("AUTH_GRPC_URL", "localhost:50051")
WS_URL = os.getenv("WS_URL", "ws://localhost:8001/ws/telemetry/")

async def run_low_trust_test():
    print("================================================================")
    print("  TEST SCENARIO: BOT BEHAVIOR, LOW TRUST, RE-AUTH & IP BLOCKING")
    print("================================================================\n")

    # 1. Connect to gRPC Auth Service
    channel = grpc.aio.insecure_channel(AUTH_GRPC_URL)
    stub = auth_pb2_grpc.AuthServiceStub(channel)

    test_username = "alice"
    test_ip = "127.0.0.1"

    # 2. Create Session via gRPC
    print(f"[1] Creating session for user '{test_username}' (IP: {test_ip})...")
    create_resp = await stub.CreateSession(auth_pb2.CreateSessionRequest(username=test_username, client_ip=test_ip))
    if not create_resp.success:
        print(f"FAILED to create session: {create_resp.message}")
        await channel.close()
        return

    session_id = create_resp.session_id
    print(f"    Session created successfully. ID: {session_id}")

    # Verify initial session state (Should be OK)
    verify_resp = await stub.VerifySession(auth_pb2.VerifySessionRequest(session_id=session_id, client_ip=test_ip))
    print(f"    Initial session status: {auth_pb2.SessionStatus.Name(verify_resp.status)} (is_valid={verify_resp.is_valid})\n")

    # ------------------------------------------------------------------
    # TEST STEP A: Send 1st Bot Telemetry packet (Expect Re-auth code 4001)
    # ------------------------------------------------------------------
    print("[2] Sending 1st packet with BOT BEHAVIOR TELEMETRY...")
    ws_endpoint = f"{WS_URL}{session_id}"
    
    async with websockets.connect(ws_endpoint) as ws:
        bot_payload = {
            "sequence_id": 0,
            "batch_timestamp": int(time.time() * 1000),
            "events": generate_bot_events()
        }
        await ws.send(json.dumps(bot_payload))
        
        try:
            msg = await ws.recv()
        except websockets.exceptions.ConnectionClosed as e:
            print(f"    -> WS Connection Closed as expected! Code: {e.code}, Reason: '{e.reason}'")
            if e.code == 4001:
                print("    SUCCESS: Received Code 4001 (Re-authentication Required)!")
            else:
                print(f"    WARNING: Unexpected close code: {e.code}")

    # Check gRPC VerifySession after 1st violation
    verify_resp = await stub.VerifySession(auth_pb2.VerifySessionRequest(session_id=session_id, client_ip=test_ip))
    print(f"    gRPC VerifySession status: {auth_pb2.SessionStatus.Name(verify_resp.status)} (Trust Score: {verify_resp.trust_score:.4f}, Message: '{verify_resp.message}')\n")

    # ------------------------------------------------------------------
    # TEST STEP B: Test Re-authentication
    # ------------------------------------------------------------------
    print("[3] Testing Re-authentication (sending password)...")
    reauth_resp = await stub.Reauthenticate(auth_pb2.ReauthenticateRequest(
        session_id=session_id, username=test_username, password="alice"
    ))
    print(f"    Re-auth result: success={reauth_resp.success}, message='{reauth_resp.message}'")
    
    verify_resp = await stub.VerifySession(auth_pb2.VerifySessionRequest(session_id=session_id, client_ip=test_ip))
    print(f"    Session status after Re-auth: {auth_pb2.SessionStatus.Name(verify_resp.status)} (Trust Score: {verify_resp.trust_score})\n")

    # ------------------------------------------------------------------
    # TEST STEP C: Trigger repeated bot violations to reach IP BLOCK (3 violations)
    # ------------------------------------------------------------------
    print("[4] Triggering repeated bot violations to test IP BLOCK...")

    for i in range(1, 4):
        print(f"    Violation Attempt #{i}...")
        try:
            async with websockets.connect(ws_endpoint) as ws:
                payload = {
                    "sequence_id": i,
                    "batch_timestamp": int(time.time() * 1000),
                    "events": generate_bot_events()
                }
                await ws.send(json.dumps(payload))
                await ws.recv()
        except websockets.exceptions.ConnectionClosed as e:
            print(f"      -> WS Closed with Code: {e.code}, Reason: '{e.reason}'")

    # Check if IP is now blocked
    print("\n[5] Verifying IP Block Status...")
    verify_resp = await stub.VerifySession(auth_pb2.VerifySessionRequest(session_id=session_id, client_ip=test_ip))
    print(f"    gRPC VerifySession status: {auth_pb2.SessionStatus.Name(verify_resp.status)} (Message: '{verify_resp.message}')")

    if verify_resp.status == auth_pb2.STATUS_IP_BLOCKED:
        print("    SUCCESS: IP Address is now BLOCKED!")
    else:
        print("    WARNING: IP was not blocked as expected.")

    await channel.close()
    print("\n================================================================")
    print("  TEST SCENARIO COMPLETED SUCCESSFULLY!")
    print("================================================================")

if __name__ == "__main__":
    asyncio.run(run_low_trust_test())
