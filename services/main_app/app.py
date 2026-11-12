import os
from pathlib import Path

import uvicorn
import grpc
from fastapi import FastAPI, Request, Form, Response, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select

from .models import User, Comment
from proto import auth_pb2, auth_pb2_grpc

# --- CONFIG ---
MAIN_DATABASE_URL = os.getenv(
    "MAIN_DATABASE_URL", 
    "postgresql+asyncpg://postgres:postgres@localhost:5432/main_app_db"
)
AUTH_GRPC_URL = os.getenv("AUTH_GRPC_URL", "localhost:50051")

engine = create_async_engine(MAIN_DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

app = FastAPI(title="Main App (Main DB)")

static_dir = Path(__file__).parent / "static"
templates_dir = Path(__file__).parent / "templates"

app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
templates = Jinja2Templates(directory=str(templates_dir))

grpc_channel = None
auth_stub = None

@app.on_event("startup")
async def startup_event():
    global grpc_channel, auth_stub
    grpc_channel = grpc.aio.insecure_channel(AUTH_GRPC_URL)
    auth_stub = auth_pb2_grpc.AuthServiceStub(grpc_channel)

@app.on_event("shutdown")
async def shutdown_event():
    if grpc_channel:
        await grpc_channel.close()

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# --- BASIC AUTH HELPERS ---

async def verify_trust_score(request: Request):
    """
    Calls Auth Service via gRPC to verify the current session is still trusted.
    Returns the username if trusted, raises HTTPException otherwise.
    """
    session_id = request.cookies.get("session_id")
    if not session_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
        
    try:
        client_ip = request.client.host if request.client else "127.0.0.1"
        resp = await auth_stub.VerifySession(
            auth_pb2.VerifySessionRequest(session_id=session_id, client_ip=client_ip)
        )
        if resp.is_valid:
            return resp.username
        else:
            if "revoked" in resp.message.lower() or "blocked" in resp.message.lower():
                raise HTTPException(status_code=403, detail=resp.message)
            else:
                raise HTTPException(status_code=401, detail=resp.message)
    except grpc.RpcError as e:
        print(f"gRPC Error: {e}")
        raise HTTPException(status_code=503, detail="Auth Service unavailable")


# --- ROUTES ---

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    session_id = request.cookies.get("session_id")
    if session_id:
        try:
            username = await verify_trust_score(request)
            return RedirectResponse(f"/user/{username}", status_code=303)
        except HTTPException:
            pass
    return RedirectResponse("/login", status_code=303)


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})


@app.post("/register")
async def register(
    username: str = Form(...), 
    password: str = Form(...), 
    db: AsyncSession = Depends(get_db)
):
    # Check local main_app_db
    user_res = await db.execute(select(User).where(User.login == username))
    if user_res.scalars().first():
        return HTMLResponse("User already exists", status_code=400)

    # Register user in main_app_db
    new_user = User(login=username, password=password)
    db.add(new_user)
    await db.commit()

    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
async def login(
    request: Request,
    username: str = Form(...), 
    password: str = Form(...), 
    db: AsyncSession = Depends(get_db)
):
    # Check credentials in local main_app_db
    user_res = await db.execute(select(User).where(User.login == username))
    user = user_res.scalars().first()

    if user and user.password == password:
        # Request Auth Service via gRPC to create session & behavioral tracking
        try:
            client_ip = request.client.host if request.client else "127.0.0.1"
            resp = await auth_stub.CreateSession(
                auth_pb2.CreateSessionRequest(username=username, client_ip=client_ip)
            )
            if resp.success:
                session_id = resp.session_id
                response = RedirectResponse(f"/user/{username}", status_code=303)
                response.set_cookie("session_id", session_id)
                response.set_cookie("username", username)
                return response
            else:
                return HTMLResponse(f"Session creation failed: {resp.message}", status_code=403)
        except grpc.RpcError:
            return HTMLResponse("Auth Service unavailable", status_code=503)

    return HTMLResponse("Invalid credentials", status_code=401)


@app.get("/user/{profile_username}", response_class=HTMLResponse)
async def profile_page(
    request: Request, 
    profile_username: str, 
    db: AsyncSession = Depends(get_db)
):
    try:
        current_user = await verify_trust_score(request)
    except HTTPException as e:
        if e.status_code == 403:
            resp = HTMLResponse(
                "<h1>403 Access Denied</h1><p>Your session was terminated due to suspicious behavioral patterns or IP block.</p><a href='/login'>Login again</a>", 
                status_code=403
            )
            resp.delete_cookie("session_id")
            return resp
        return RedirectResponse("/login", status_code=303)

    user_res = await db.execute(select(User).where(User.login == profile_username))
    if not user_res.scalars().first():
        return HTMLResponse("User not found", status_code=404)

    comments_res = await db.execute(select(Comment).where(Comment.target_user_login == profile_username))
    comment_texts = [c.text for c in comments_res.scalars().all()]

    users_res = await db.execute(select(User.login))
    user_list = users_res.scalars().all()

    return templates.TemplateResponse("profile.html", {
        "request": request,
        "current_user": current_user,
        "profile_username": profile_username,
        "comments": comment_texts,
        "user_list": user_list,
        "can_comment": (profile_username != current_user)
    })


@app.post("/comment/{profile_username}")
async def add_comment(
    request: Request, 
    profile_username: str, 
    db: AsyncSession = Depends(get_db)
):
    try:
        current_user = await verify_trust_score(request)
    except HTTPException:
        return JSONResponse(status_code=401, content={"status": "error", "message": "Unauthorized or revoked"})

    if profile_username == current_user:
        return {"status": "error", "message": "Cannot comment on self"}

    data = await request.json()
    text = data.get("text", "")
    if text:
        new_comment = Comment(target_user_login=profile_username, author_login=current_user, text=text)
        db.add(new_comment)
        await db.commit()

    return {"status": "ok"}


@app.post("/logout")
async def logout(request: Request):
    session_id = request.cookies.get("session_id")
    if session_id:
        try:
            await auth_stub.Logout(auth_pb2.LogoutRequest(session_id=session_id))
        except grpc.RpcError:
            pass
            
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("session_id")
    response.delete_cookie("username")
    return response


if __name__ == "__main__":
    uvicorn.run("services.main_app.app:app", host="0.0.0.0", port=8000, reload=True)
