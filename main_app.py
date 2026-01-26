import uvicorn
import httpx
from fastapi import FastAPI, Request, Form, Response, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select
from models import User, Comment

# --- CONFIG ---
DATABASE_URL = "postgresql+asyncpg://postgres:1111@localhost:5432/behavioural_auth"
AUTH_SERVICE_URL = "http://localhost:8001"

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

app = FastAPI(title="Main App (Resource)")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# --- ВЗАИМОДЕЙСТВИЕ С AUTH SERVICE ---

async def check_auth_status(request: Request):
    """
    Отправляет запрос в Auth Service.
    Auth Service сам решит: обучать профиль (если новый) или проверять (если старый).
    """
    session_id = request.cookies.get("session_id")
    if not session_id:
        raise HTTPException(status_code=401, detail="No session")

    async with httpx.AsyncClient() as client:
        try:
            # Мы просто говорим: "Проверь сессию".
            # Вся логика "Обучение vs Защита" скрыта внутри этого вызова.
            response = await client.post(
                f"{AUTH_SERVICE_URL}/internal/verify_session",
                json={"session_id": session_id}
            )

            if response.status_code == 200:
                return response.json()  # Возвращает {username, mode, score}

            elif response.status_code == 403:
                # Auth Service решил, что поведение подозрительное
                raise HTTPException(status_code=403, detail="Security Alert: Behavior mismatch")
            else:
                raise HTTPException(status_code=401, detail="Session invalid")

        except httpx.RequestError:
            raise HTTPException(status_code=503, detail="Auth Service Unavailable")


# --- ROUTES ---

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    try:
        auth_data = await check_auth_status(request)
        return RedirectResponse(f"/user/{auth_data['username']}", status_code=303)
    except HTTPException:
        return RedirectResponse("/login", status_code=303)


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})


@app.post("/register")
async def register(username: str = Form(...), password: str = Form(...)):
    # 1. Просто создаем юзера в Auth Service
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{AUTH_SERVICE_URL}/internal/register",
                                 json={"username": username, "password": password})
        if resp.status_code != 200:
            return HTMLResponse("Registration failed (User exists)", status_code=400)

    # 2. Перенаправляем на логин (или можно сразу логинить, но по классике на логин)
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
async def login(username: str = Form(...), password: str = Form(...)):
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{AUTH_SERVICE_URL}/internal/login",
                                 json={"username": username, "password": password})

        if resp.status_code == 200:
            data = resp.json()
            response = RedirectResponse(f"/user/{username}", status_code=303)
            response.set_cookie("session_id", data["session_id"])
            return response
        else:
            return HTMLResponse("Invalid credentials", status_code=401)


@app.get("/user/{profile_username}", response_class=HTMLResponse)
async def profile_page(request: Request, profile_username: str, db: AsyncSession = Depends(get_db)):
    # --- МОМЕНТ ИСТИНЫ ---
    # При загрузке страницы происходит обращение к Auth Service.
    # 1. Если это первый вход после регистрации -> Auth Service создаст профиль (Learning).
    # 2. Если это n-ный вход -> Auth Service проверит поведение (Protection).

    try:
        auth_data = await check_auth_status(request)
        current_user = auth_data['username']
        security_mode = auth_data.get('mode', 'UNKNOWN')  # LEARNING или PROTECTION
        trust_score = auth_data.get('score', 1.0)
    except HTTPException as e:
        if e.status_code == 403:
            resp = HTMLResponse(
                "<h1>403 Access Denied</h1>"
                "<p style='color:red'>Система обнаружила аномальное поведение, отличное от вашего профиля.</p>"
                "<a href='/login'>Войти заново</a>",
                status_code=403
            )
            resp.delete_cookie("session_id")
            return resp
        return RedirectResponse("/login", status_code=303)

    # Загрузка данных страницы
    user_res = await db.execute(select(User).where(User.login == profile_username))
    if not user_res.scalars().first():
        return HTMLResponse("User not found", status_code=404)

    comments_res = await db.execute(select(Comment).where(Comment.target_user_login == profile_username))
    comment_texts = [c.text for c in comments_res.scalars().all()]

    users_res = await db.execute(select(User.login))
    user_list = users_res.scalars().all()

    # Показываем статус безопасности прямо на странице для наглядности
    system_status = f"System Mode: {security_mode} | Trust Score: {trust_score}"

    return templates.TemplateResponse("profile.html", {
        "request": request,
        "current_user": current_user,
        "profile_username": profile_username,
        "comments": comment_texts,
        "user_list": user_list,
        "can_comment": (profile_username != current_user),
        "system_status": system_status
    })


@app.post("/comment/{profile_username}")
async def add_comment(request: Request, profile_username: str, db: AsyncSession = Depends(get_db)):
    # При отправке комментария тоже идет проверка
    try:
        auth_data = await check_auth_status(request)
        current_user = auth_data['username']
    except HTTPException:
        return JSONResponse(status_code=403, content={"status": "error", "message": "Behavior check failed"})

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
        async with httpx.AsyncClient() as client:
            await client.post(f"{AUTH_SERVICE_URL}/internal/logout", json={"session_id": session_id})

    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("session_id")
    return response


if __name__ == "__main__":
    uvicorn.run("main_app:app", host="0.0.0.0", port=8000, reload=True)