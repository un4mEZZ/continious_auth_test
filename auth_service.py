import uvicorn
import secrets
import random
from fastapi import FastAPI, Depends, HTTPException, Body
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select
from models import Base, User, BehaviorProfile, AuthEvent

# --- CONFIG ---
DATABASE_URL = "postgresql+asyncpg://postgres:1111@localhost:5432/behavioural_auth"
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

app = FastAPI(title="Auth & Behavior Service")

# In-memory sessions store (Redis in production)
sessions = {}


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# --- LOGIC ---

def calculate_trust_score(baseline: dict, current: dict) -> float:
    if not baseline or not current: return 0.0
    base_mouse = baseline.get("avgMouseSpeed", 100)
    base_key = baseline.get("avgKeyPressTime", 0.2)
    curr_mouse = current.get("avgMouseSpeed", 100)
    curr_key = current.get("avgKeyPressTime", 0.2)

    mouse_diff = abs(base_mouse - curr_mouse) / base_mouse
    key_diff = abs(base_key - curr_key) / base_key
    avg_diff = (mouse_diff + key_diff) / 2
    return round(max(0.0, 1.0 - avg_diff), 2)


async def record_behavior_pattern(user_uuid: str, db: AsyncSession):
    """Генерация эталона при входе"""
    behavior_vector = {
        "avgMouseSpeed": random.uniform(50, 300),
        "avgKeyPressTime": random.uniform(0.05, 0.35)
    }
    result = await db.execute(select(BehaviorProfile).where(BehaviorProfile.user_id == user_uuid))
    profile = result.scalars().first()

    if profile:
        profile.features = behavior_vector
    else:
        db.add(BehaviorProfile(user_id=user_uuid, features=behavior_vector))
    await db.commit()


# --- API ENDPOINTS ---

@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        # Создаем таблицы, если их нет
        await conn.run_sync(Base.metadata.create_all)


@app.post("/internal/register")
async def register_user(data: dict = Body(...), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.login == data['username']))
    if result.scalars().first():
        raise HTTPException(status_code=400, detail="User exists")

    new_user = User(login=data['username'], password=data['password'])
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    # Сразу создаем профиль
    await record_behavior_pattern(new_user.uuid, db)
    return {"status": "created"}


@app.post("/internal/login")
async def login_user(data: dict = Body(...), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.login == data['username']))
    user = result.scalars().first()

    if not user or user.password != data['password']:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Создаем сессию
    session_id = secrets.token_hex(16)
    sessions[session_id] = user.login

    # Обновляем паттерн
    await record_behavior_pattern(user.uuid, db)

    return {"session_id": session_id, "username": user.login}


@app.post("/internal/verify_session")
async def verify_session(data: dict = Body(...), db: AsyncSession = Depends(get_db)):
    """
    Это 'Яндекс.Метрика' часть.
    Принимает ID сессии, симулирует сбор данных, проверяет аномалии.
    """
    session_id = data.get("session_id")
    if not session_id or session_id not in sessions:
        raise HTTPException(status_code=401, detail="Invalid session")

    username = sessions[session_id]

    # 1. Получаем эталон
    user_res = await db.execute(select(User).where(User.login == username))
    user = user_res.scalars().first()

    prof_res = await db.execute(select(BehaviorProfile).where(BehaviorProfile.user_id == user.uuid))
    profile = prof_res.scalars().first()

    if not profile or not profile.features:
        return {"status": "allowed", "username": username, "score": 1.0}  # Без профиля пропускаем

    baseline = profile.features

    # 2. СИМУЛЯЦИЯ (Сбор данных с клиента)
    is_anomaly = random.random() < 0.2
    if is_anomaly:
        current = {
            "avgMouseSpeed": baseline["avgMouseSpeed"] * random.uniform(2.0, 3.0),
            "avgKeyPressTime": baseline["avgKeyPressTime"] * random.uniform(0.1, 0.2)
        }
        print(f"[AUTH-SERVER] ANOMALY DETECTED for {username}")
    else:
        current = {
            "avgMouseSpeed": baseline["avgMouseSpeed"] * random.uniform(0.8, 1.2),
            "avgKeyPressTime": baseline["avgKeyPressTime"] * random.uniform(0.8, 1.2)
        }

    # 3. Расчет
    trust_score = calculate_trust_score(baseline, current)
    decision = trust_score >= 0.5

    # 4. Лог события
    db.add(AuthEvent(user_id=user.uuid, score=trust_score, decision=decision))
    await db.commit()

    if not decision:
        # Если скор низкий, удаляем сессию
        del sessions[session_id]
        raise HTTPException(status_code=403, detail="Suspicious behavior")

    return {"status": "allowed", "username": username, "score": trust_score}


@app.post("/internal/logout")
async def logout(data: dict = Body(...)):
    session_id = data.get("session_id")
    if session_id in sessions:
        del sessions[session_id]
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("auth_service:app", host="0.0.0.0", port=8001, reload=True)