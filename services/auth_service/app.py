import os
import sys
import asyncio
import json
import uuid
from pathlib import Path
from typing import List, Dict, Any, Optional

import grpc
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import uvicorn
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, desc

# Import models & statistical scoring engines
from .models import BehaviorProfile, AuthEvent
from .scoring import FeatureExtractor, BehaviorEvaluator, ProfileEngine
from tests.test_low_score import generate_bot_events

# Import protobuf / gRPC definitions
from proto import auth_pb2, auth_pb2_grpc

# --- CONFIG ---
AUTH_DATABASE_URL = os.getenv(
    "AUTH_DATABASE_URL", 
    "postgresql+asyncpg://postgres:postgres@localhost:5432/auth_service_db"
)
GRPC_PORT = int(os.getenv("AUTH_GRPC_PORT", "50051"))

engine = create_async_engine(AUTH_DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# In-memory active session tracking
ACTIVE_SESSIONS: Dict[str, Dict[str, Any]] = {}
USER_SESSIONS: Dict[str, str] = {}  # Maps username -> session_id for Single Session Policy
BLOCKED_IPS = set()


# =====================================================================
# --- gRPC SERVICE IMPLEMENTATION ---
# =====================================================================

class AuthServiceServicer(auth_pb2_grpc.AuthServiceServicer):
    
    async def CreateSession(self, request, context):
        username = request.username
        client_ip = request.client_ip or "127.0.0.1"

        if client_ip in BLOCKED_IPS:
            return auth_pb2.CreateSessionResponse(success=False, session_id="", message="IP Blocked")

        if not username:
            return auth_pb2.CreateSessionResponse(success=False, session_id="", message="Username is required")

        # Check or create behavior profile in DB
        async with AsyncSessionLocal() as db:
            profile_res = await db.execute(select(BehaviorProfile).where(BehaviorProfile.username == username))
            profile = profile_res.scalars().first()
            if not profile:
                new_profile = BehaviorProfile(
                    username=username, 
                    features={"status": "learning", "events_collected": 0}
                )
                db.add(new_profile)
                await db.commit()

        # SINGLE SESSION POLICY: Revoke previous session if it exists
        if username in USER_SESSIONS:
            old_session = USER_SESSIONS[username]
            if old_session in ACTIVE_SESSIONS:
                print(f"[AuthService] Single-Session Policy: Invalidating old session {old_session} for user '{username}'")
                del ACTIVE_SESSIONS[old_session]

        session_id = str(uuid.uuid4())
        USER_SESSIONS[username] = session_id
        
        ACTIVE_SESSIONS[session_id] = {
            "username": username,
            "status": "active",
            "trust_score": 1.0,
            "last_sequence": -1,
            "low_trust_count": 0,
            "client_ip": client_ip,
            "algorithm_scores": {
                "interval": 1.0,
                "mahalanobis": 1.0,
                "cosine_dynamic": 1.0
            },
            "active_algorithm": BehaviorEvaluator.ACTIVE_ALGORITHM
        }
        print(f"[AuthService] Session created: {session_id} for user '{username}' (IP: {client_ip})")
        return auth_pb2.CreateSessionResponse(success=True, session_id=session_id, message="Session created")

    async def VerifySession(self, request, context):
        client_ip = request.client_ip or "127.0.0.1"

        if client_ip in BLOCKED_IPS:
            return auth_pb2.VerifySessionResponse(
                is_valid=False, 
                username="", 
                trust_score=0.0, 
                message="IP Address is blocked", 
                status=auth_pb2.STATUS_IP_BLOCKED
            )

        session_data = ACTIVE_SESSIONS.get(request.session_id)
        if not session_data:
            return auth_pb2.VerifySessionResponse(
                is_valid=False, 
                username="", 
                trust_score=0.0, 
                message="Session not found or expired", 
                status=auth_pb2.STATUS_REVOKED
            )
        
        status_str = session_data.get("status", "active")

        if status_str == "reauth_required":
            return auth_pb2.VerifySessionResponse(
                is_valid=False,
                username=session_data["username"],
                trust_score=session_data["trust_score"],
                message="Re-authentication required due to low trust score",
                status=auth_pb2.STATUS_REAUTH_REQUIRED
            )

        if status_str == "ip_blocked":
            return auth_pb2.VerifySessionResponse(
                is_valid=False,
                username=session_data["username"],
                trust_score=session_data["trust_score"],
                message="IP Address is blocked",
                status=auth_pb2.STATUS_IP_BLOCKED
            )

        if status_str == "revoked":
            return auth_pb2.VerifySessionResponse(
                is_valid=False, 
                username=session_data["username"], 
                trust_score=session_data["trust_score"], 
                message="Session revoked due to low trust score",
                status=auth_pb2.STATUS_REVOKED
            )
            
        return auth_pb2.VerifySessionResponse(
            is_valid=True, 
            username=session_data["username"], 
            trust_score=session_data["trust_score"], 
            message="OK",
            status=auth_pb2.STATUS_OK
        )

    async def Reauthenticate(self, request, context):
        session_id = request.session_id
        session_data = ACTIVE_SESSIONS.get(session_id)
        
        if not session_data:
            return auth_pb2.ReauthenticateResponse(success=False, message="Invalid session")

        if session_data["client_ip"] in BLOCKED_IPS:
            return auth_pb2.ReauthenticateResponse(success=False, message="IP Blocked")

        # Reset trust score and session status
        session_data["status"] = "active"
        session_data["trust_score"] = 1.0
        session_data["low_trust_count"] = 0
        session_data["algorithm_scores"] = {
            "interval": 1.0,
            "mahalanobis": 1.0,
            "cosine_dynamic": 1.0
        }
        print(f"[AuthService] User '{session_data['username']}' re-authenticated successfully! Resetting Trust Score to 1.0.")
        return auth_pb2.ReauthenticateResponse(success=True, message="Re-authenticated successfully")

    async def Logout(self, request, context):
        if request.session_id in ACTIVE_SESSIONS:
            username = ACTIVE_SESSIONS[request.session_id]["username"]
            del ACTIVE_SESSIONS[request.session_id]
            if USER_SESSIONS.get(username) == request.session_id:
                del USER_SESSIONS[username]
        return auth_pb2.LogoutResponse(success=True)


async def serve_grpc():
    server = grpc.aio.server()
    auth_pb2_grpc.add_AuthServiceServicer_to_server(AuthServiceServicer(), server)
    server.add_insecure_port(f'[::]:{GRPC_PORT}')
    await server.start()
    print(f"gRPC server running on port {GRPC_PORT} (Auth Service DB)")
    await server.wait_for_termination()


# =====================================================================
# --- FASTAPI WEBSOCKET TELEMETRY & ADMIN SERVER ---
# =====================================================================

app = FastAPI(title="Continuous Auth Behavioral Biometrics Service")

templates_dir = Path(__file__).parent / "templates"
auth_templates = Jinja2Templates(directory=str(templates_dir))

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(serve_grpc())

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request, db: AsyncSession = Depends(get_db)):
    events_res = await db.execute(select(AuthEvent).order_by(desc(AuthEvent.ts)).limit(100))
    events = events_res.scalars().all()
    
    profiles_res = await db.execute(select(BehaviorProfile))
    profiles = profiles_res.scalars().all()
    
    return auth_templates.TemplateResponse("admin.html", {
        "request": request,
        "events": events,
        "profiles": profiles,
        "active_sessions": ACTIVE_SESSIONS,
        "active_algorithm": BehaviorEvaluator.ACTIVE_ALGORITHM
    })

@app.post("/admin/algorithm", response_class=HTMLResponse)
async def set_algorithm(request: Request):
    form = await request.form()
    algo = form.get("algorithm") or request.query_params.get("algorithm")
    if algo and BehaviorEvaluator.set_active_algorithm(algo):
        for s in ACTIVE_SESSIONS.values():
            s["active_algorithm"] = algo
            if "algorithm_scores" in s and algo in s["algorithm_scores"]:
                s["trust_score"] = s["algorithm_scores"][algo]
        print(f"[AuthService] Active scoring algorithm changed to: {algo}")

    algo_labels = {
        "interval": "Интервальная оценка (k-сигм)",
        "mahalanobis": "Расстояние Махаланобиса",
        "cosine_dynamic": "Косинусное сходство (EMA)"
    }
    label = algo_labels.get(BehaviorEvaluator.ACTIVE_ALGORITHM, BehaviorEvaluator.ACTIVE_ALGORITHM)
    return HTMLResponse(
        f'<span class="badge badge-success" id="active-algo-badge">Активный алгоритм: {label}</span>'
    )

@app.post("/admin/test-bot/{username}", response_class=HTMLResponse)
async def admin_test_bot(username: str, db: AsyncSession = Depends(get_db)):
    """
    Ручной запуск проверки нелегитимного ("ботовидного") поведения для выбранного пользователя.
    """
    profile_res = await db.execute(select(BehaviorProfile).where(BehaviorProfile.username == username))
    profile = profile_res.scalars().first()
    if not profile:
        return HTMLResponse(
            f'<div class="alert alert-danger" style="margin-top: 1rem; padding: 0.75rem 1rem; border-radius: 8px; background: rgba(239, 68, 68, 0.2); border: 1px solid var(--danger); color: #fca5a5;">'
            f'Пользователь <strong>{username}</strong> не найден в базе профилей.'
            f'</div>'
        )

    # 1. Генерация нелегитимных ботовидных событий
    bot_events = generate_bot_events()
    bot_features = FeatureExtractor.extract_features(bot_events)

    # 2. Статистический скоринг по всем 3 алгоритмам
    current_score = 1.0
    user_session_id = USER_SESSIONS.get(username)
    session_data = ACTIVE_SESSIONS.get(user_session_id) if user_session_id else None
    if session_data:
        current_score = session_data.get("trust_score", 1.0)

    new_score, algo_scores = await BehaviorEvaluator.evaluate_trust(
        username, bot_features, current_score, profile, return_all_scores=True
    )

    # 3. Фиксация в профиле и аудит-логе
    await ProfileEngine.update_profile(username, bot_features, new_score, db)
    evt = AuthEvent(
        username=username,
        score=new_score,
        decision=(new_score >= 0.4)
    )
    db.add(evt)
    await db.commit()

    # 4. Если есть активная сессия, применяем ступенчатую защиту
    session_msg = "Сессия не активна (тест выполнен против эталона профиля)"
    if session_data:
        session_data["trust_score"] = new_score
        session_data["algorithm_scores"] = algo_scores
        if new_score < 0.4:
            session_data["low_trust_count"] = session_data.get("low_trust_count", 0) + 1
            violations = session_data["low_trust_count"]
            if violations >= 3:
                session_data["status"] = "ip_blocked"
                BLOCKED_IPS.add(session_data.get("client_ip", "127.0.0.1"))
                session_msg = f"Сессия {user_session_id[:8]}... СТАТУС: IP ЗАБЛОКИРОВАН (3 нарушения)!"
            else:
                session_data["status"] = "reauth_required"
                session_msg = f"Сессия {user_session_id[:8]}... СТАТУС: ТРЕБУЕТСЯ ПОВТОРНАЯ АУТЕНТИФИКАЦИЯ (код 4001)!"

    return HTMLResponse(
        f'<div class="alert alert-warning" style="margin-top: 1rem; padding: 0.75rem 1rem; border-radius: 8px; background: rgba(234, 179, 8, 0.15); border: 1px solid #eab308; color: #fde047;">'
        f'🤖 <strong>Тест нелегитимного бота для пользователя "{username}":</strong><br/>'
        f'Trust Score: <strong>{new_score:.4f}</strong> (Интервал: {algo_scores["interval"]:.4f}, '
        f'Махаланобис: {algo_scores["mahalanobis"]:.4f}, Косинус EMA: {algo_scores["cosine_dynamic"]:.4f})<br/>'
        f'<span style="font-size: 0.85rem; color: #cbd5e1;">{session_msg}</span>'
        f'</div>'
    )

@app.websocket("/ws/telemetry/{session_id}")
async def websocket_telemetry(websocket: WebSocket, session_id: str):
    client_ip = websocket.client.host if websocket.client else "127.0.0.1"

    if client_ip in BLOCKED_IPS:
        await websocket.accept()
        await websocket.close(code=4003, reason="IP Blocked")
        return

    await websocket.accept()
    
    session_data = ACTIVE_SESSIONS.get(session_id)
    if not session_data:
        await websocket.close(code=1008, reason="Invalid session")
        return
        
    username = session_data['username']
    session_data["last_sequence"] = -1
    print(f"WebSocket telemetry connected: User '{username}' (Session {session_id}, IP: {client_ip})")
    
    try:
        while True:
            data = await websocket.receive_text()
            payload = json.loads(data)
            
            # Check Single Session Policy
            if session_id not in ACTIVE_SESSIONS:
                print(f"[WS Warning] Session {session_id} was superseded by a newer login for '{username}'. Closing.")
                await websocket.close(code=1008, reason="Session superseded")
                break
            
            seq_id = payload.get("sequence_id", 0)
            if seq_id <= session_data.get("last_sequence", -1):
                continue
            
            session_data["last_sequence"] = seq_id
            raw_events = payload.get("events", [])
            forced_score = payload.get("force_trust_score", None)
            
            # 1. Feature Extraction (5 biometric features)
            features = FeatureExtractor.extract_features(raw_events)
            
            # 2. Statistical Scoring & Biometric Trust Inference
            async with AsyncSessionLocal() as db:
                profile_res = await db.execute(select(BehaviorProfile).where(BehaviorProfile.username == username))
                profile = profile_res.scalars().first()
                
                current_score = session_data["trust_score"]
                new_score, algo_scores = await BehaviorEvaluator.evaluate_trust(
                    username, features, current_score, profile, 
                    forced_score=forced_score, return_all_scores=True
                )
                session_data["trust_score"] = new_score
                session_data["algorithm_scores"] = algo_scores
                session_data["active_algorithm"] = BehaviorEvaluator.ACTIVE_ALGORITHM
                
                # 3. Model Engine updating
                await ProfileEngine.update_profile(username, features, new_score, db)
                
                # 4. Audit Log in auth_service_db
                evt = AuthEvent(
                    username=username,
                    score=new_score,
                    decision=(new_score >= 0.4)
                )
                db.add(evt)
                await db.commit()

            # Stepped Security Enforcement
            if new_score < 0.4:
                session_data["low_trust_count"] += 1
                violations = session_data["low_trust_count"]
                
                if violations >= 3:
                    session_data["status"] = "ip_blocked"
                    BLOCKED_IPS.add(client_ip)
                    print(f"[SECURITY ALERT] IP '{client_ip}' BLOCKED due to {violations} repeated low trust violations! (Score: {new_score:.2f})")
                    await websocket.close(code=4003, reason="IP Blocked")
                    break
                else:
                    session_data["status"] = "reauth_required"
                    print(f"[ALERT] Session {session_id} for user '{username}' REAUTH REQUIRED! Violation #{violations} (Score: {new_score:.2f})")
                    await websocket.close(code=4001, reason="Re-authentication required")
                    break

    except WebSocketDisconnect:
        print(f"WebSocket telemetry disconnected: User '{username}'")
    except json.JSONDecodeError:
        await websocket.close(code=1003, reason="Invalid JSON")


if __name__ == "__main__":
    uvicorn.run("services.auth_service.app:app", host="0.0.0.0", port=8001, reload=True)
