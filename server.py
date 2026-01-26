import os
import secrets

import random
import math

from datetime import datetime
from uuid import uuid4, UUID
from typing import List, Optional

from fastapi import FastAPI, Request, Form, Response, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import uvicorn

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, String, DateTime, ForeignKey, Text, select, Integer, Float, Boolean
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID

# --- КОНФИГУРАЦИЯ БАЗЫ ДАННЫХ ---
# Замените на ваши данные подключения: user:password@host:port/dbname
DATABASE_URL = "postgresql+asyncpg://postgres:1111@localhost:5432/behavioural_auth"

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


# --- МОДЕЛИ ДАННЫХ (ORM) ---

class User(Base):
    __tablename__ = "users"
    __table_args__ = {"schema": "auth"}

    uuid = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    login = Column(String, unique=True, nullable=False)
    password = Column(String, nullable=False)  # Храним в открытом виде (согласно ТЗ)
    #created_at = Column(DateTime, default=datetime.now(datetime.UTC))
    created_at = Column(DateTime, default=datetime.utcnow)


class Comment(Base):
    __tablename__ = "comments"
    __table_args__ = {"schema": "auth"}

    # Исправлено: используем Integer, убираем default (Postgres сам сделает автоинкремент)
    id = Column(Integer, primary_key=True, index=True)

    target_user_login = Column(String, ForeignKey("auth.users.login"), nullable=False)
    author_login = Column(String, nullable=False)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class BehaviorProfile(Base):
    __tablename__ = "behavior_profiles"
    __table_args__ = {"schema": "auth"}

    # Используем user_id как Primary Key (связь 1 к 1)
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.uuid"), primary_key=True)
    features = Column(JSONB)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class AuthEvent(Base):
    __tablename__ = "auth_events"
    __table_args__ = {"schema": "auth"}

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.uuid"), nullable=False)
    score = Column(Float)
    decision = Column(Boolean)
    ts = Column(DateTime, default=datetime.utcnow)

# --- ПРИЛОЖЕНИЕ ---

app = FastAPI(title="Continuous Auth System")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Простой in-memory кэш сессий (для продакшена лучше Redis)
sessions = {}


# Dependency для получения сессии БД
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# --- ФУНКЦИИ БЕЗОПАСНОСТИ (ИЗ БЛОК-СХЕМЫ) ---

def calculate_trust_score(baseline: dict, current: dict) -> float:
    """
    Сравнивает эталонный профиль с текущим вектором.
    Возвращает число от 0.0 до 1.0.
    """
    if not baseline or not current:
        return 0.0

    # Получаем значения
    base_mouse = baseline.get("avgMouseSpeed", 100)
    base_key = baseline.get("avgKeyPressTime", 0.2)

    curr_mouse = current.get("avgMouseSpeed", 100)
    curr_key = current.get("avgKeyPressTime", 0.2)

    # Считаем отклонение в процентах (0.0 = совпадение, 1.0 = 100% разницы)
    # abs(100 - 120) / 100 = 0.2 (20% разницы)
    mouse_diff = abs(base_mouse - curr_mouse) / base_mouse
    key_diff = abs(base_key - curr_key) / base_key

    # Среднее отклонение
    avg_diff = (mouse_diff + key_diff) / 2

    # Формула скора: Чем меньше отклонение, тем выше скор.
    # Если отклонение > 100%, скор уходит в 0.
    # Пример: отклонение 0.1 (10%) -> score 0.9
    score = max(0.0, 1.0 - avg_diff)

    return round(score, 2)


async def record_behavior_pattern(user: User, request: Request, db: AsyncSession):
    """
    ЭТАП 1: Генерация эталонного вектора при входе/регистрации.
    """
    # Генерируем рандомный "почерк" пользователя
    # avgMouseSpeed: пикселей в секунду
    # avgKeyPressTime: секунд на нажатие
    behavior_vector = {
        "avgMouseSpeed": random.uniform(50, 300),
        "avgKeyPressTime": random.uniform(0.05, 0.35)
    }

    print(f"[LEARNING] Создан профиль для {user.login}: {behavior_vector}")

    # Ищем, есть ли уже профиль
    result = await db.execute(select(BehaviorProfile).where(BehaviorProfile.user_id == user.uuid))
    profile = result.scalars().first()

    if profile:
        profile.features = behavior_vector  # Обновляем эталон при новом логине
    else:
        new_profile = BehaviorProfile(user_id=user.uuid, features=behavior_vector)
        db.add(new_profile)

    await db.commit()


async def continuous_authentication_check(session_id: str, request: Request, db: AsyncSession) -> bool:
    """
    ЭТАП 2, 3, 4: Симуляция текущего поведения, расчет скора и принятие решения.
    """
    if session_id not in sessions:
        return False

    username = sessions[session_id]

    # 1. Получаем пользователя и его ЭТАЛОННЫЙ профиль из БД
    result = await db.execute(select(User).where(User.login == username))
    user = result.scalars().first()
    if not user: return False

    prof_result = await db.execute(select(BehaviorProfile).where(BehaviorProfile.user_id == user.uuid))
    profile = prof_result.scalars().first()

    # Если профиля нет (баг), пускаем, но пишем алерт
    if not profile or not profile.features:
        return True

    baseline_features = profile.features

    # 2. СИМУЛЯЦИЯ: Генерируем "текущие" данные, которые якобы пришли от браузера
    # Сделаем так:
    # - 80% шанс, что поведение нормальное (небольшое отклонение от эталона)
    # - 20% шанс, что поведение аномальное (симуляция атаки/бота)
    is_anomaly = random.random() < 0.2

    if is_anomaly:
        # Аномалия: значения отличаются в 2-3 раза
        current_features = {
            "avgMouseSpeed": baseline_features["avgMouseSpeed"] * random.uniform(2.0, 3.0),
            "avgKeyPressTime": baseline_features["avgKeyPressTime"] * random.uniform(0.1, 0.2)
            # Слишком быстро печатает
        }
        print(f"[SIMULATION] !!! АНОМАЛИЯ ПОВЕДЕНИЯ !!!")
    else:
        # Норма: отклонение +/- 20%
        current_features = {
            "avgMouseSpeed": baseline_features["avgMouseSpeed"] * random.uniform(0.8, 1.2),
            "avgKeyPressTime": baseline_features["avgKeyPressTime"] * random.uniform(0.8, 1.2)
        }

    # 3. Вычисляем Trust Score
    trust_score = calculate_trust_score(baseline_features, current_features)

    # 4. Принимаем решение
    threshold = 0.5
    decision = trust_score >= threshold

    # Логируем событие в БД
    event = AuthEvent(
        user_id=user.uuid,
        score=trust_score,
        decision=decision
    )
    db.add(event)
    await db.commit()

    print(f"[AUTH CHECK] User: {username} | Score: {trust_score} | Decision: {'PASS' if decision else 'BLOCK'}")

    return decision

# --- ЭНДПОИНТЫ ---

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, db: AsyncSession = Depends(get_db)): # Добавлен Depends
    session_id = request.cookies.get("session_id")
    if session_id and session_id in sessions:
        # Передаем db
        if await continuous_authentication_check(session_id, request, db):
            username = sessions[session_id]
            return RedirectResponse(f"/user/{username}", status_code=303)
    return RedirectResponse("/login", status_code=303)


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})


@app.post("/register")
async def register(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        db: AsyncSession = Depends(get_db)
):
    # Проверка существования пользователя
    result = await db.execute(select(User).where(User.login == username))
    if result.scalars().first():
        return HTMLResponse("Username already exists", status_code=400)

    # Создание пользователя (пароль в открытом виде согласно ТЗ)
    new_user = User(login=username, password=password)
    db.add(new_user)

    # Сразу записываем паттерн при регистрации (опционально)
    await record_behavior_pattern(new_user, request, db)

    await db.commit()

    # Автоматический логин
    session_id = secrets.token_hex(16)
    sessions[session_id] = username

    response = RedirectResponse("/login", status_code=303)
    response.set_cookie("session_id", session_id)
    return response


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
    # БЛОК СХЕМА: Логин есть в БД?
    result = await db.execute(select(User).where(User.login == username))
    user = result.scalars().first()

    if not user:
        # Ветвь "нет" -> Неверные данные
        return HTMLResponse("Invalid username or password", status_code=401)

    # БЛОК СХЕМА: Вычислить хэш (здесь сравнение plaintext, т.к. в БД plaintext)
    # Ветвь "да" -> Хэш соответствует?
    if user.password != password:
        return HTMLResponse("Invalid password", status_code=401)

    # БЛОК СХЕМА: Успешная авторизация
    session_id = secrets.token_hex(16)
    sessions[session_id] = username

    # БЛОК СХЕМА: Запись паттерна поведения
    await record_behavior_pattern(user, request, db)

    response = RedirectResponse(f"/user/{username}", status_code=303)
    response.set_cookie("session_id", session_id)
    return response


@app.get("/user/{profile_username}", response_class=HTMLResponse)
async def profile_page(request: Request, profile_username: str, db: AsyncSession = Depends(get_db)):
    session_id = request.cookies.get("session_id")

    # ПРОВЕРКА
    is_allowed = await continuous_authentication_check(session_id, request, db)

    if not is_allowed:
        # ЭТАП 4: Сброс сессии и 403
        if session_id in sessions:
            del sessions[session_id]  # Удаляем сессию

        response = HTMLResponse(
            content="<h1>403 Forbidden</h1><p>Suspicious behavior detected. Access denied.</p><a href='/login'>Login again</a>",
            status_code=403)
        response.delete_cookie("session_id")
        return response

    current_user = sessions.get(session_id)

    # Получаем данные профиля
    result = await db.execute(select(User).where(User.login == profile_username))
    if not result.scalars().first():
        return HTMLResponse("User not found", status_code=404)

    # Получаем комментарии
    comments_result = await db.execute(select(Comment).where(Comment.target_user_login == profile_username))
    comments = comments_result.scalars().all()

    comment_texts = [c.text for c in comments]

    # Список всех пользователей для навигации
    users_result = await db.execute(select(User.login))
    user_list = users_result.scalars().all()

    can_comment = (profile_username != current_user)

    # БЛОК СХЕМА: Полный доступ к ресурсу
    return templates.TemplateResponse("profile.html", {
        "request": request,
        "current_user": current_user,
        "profile_username": profile_username,
        "comments": comment_texts,
        "user_list": user_list,
        "can_comment": can_comment
    })


@app.post("/comment/{profile_username}")
async def add_comment(request: Request, profile_username: str, db: AsyncSession = Depends(get_db)):
    session_id = request.cookies.get("session_id")

    is_allowed = await continuous_authentication_check(session_id, request, db)

    if not is_allowed:
        # Сброс сессии
        if session_id in sessions:
            del sessions[session_id]

        # Возвращаем JSON ошибку для fetch-запроса
        return JSONResponse(
            status_code=403,
            content={"status": "error", "message": "403 Forbidden: Low trust score"}
        )

    current_user = sessions.get(session_id)

    # Проверки логики
    if not current_user or profile_username == current_user:
        return {"status": "error", "message": "Cannot comment"}

    data = await request.json()
    text = data.get("text", "")

    if text:
        new_comment = Comment(
            target_user_login=profile_username,
            author_login=current_user,
            text=text
        )
        db.add(new_comment)
        await db.commit()

    return {"status": "ok"}


@app.post("/logout")
async def logout(response: Response, request: Request):
    session_id = request.cookies.get("session_id")
    if session_id in sessions:
        del sessions[session_id]
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("session_id")
    return response


if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)