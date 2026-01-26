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

app = FastAPI(title="Auth Service (Brain)")
sessions = {}  # session_id -> user_login


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# --- МАТЕМАТИКА ПОВЕДЕНИЯ ---

def generate_simulation_data(is_anomaly=False, baseline=None):
    """
    Симулирует данные, приходящие от 'браузера' (движение мыши, клавиатура).
    """
    if baseline and not is_anomaly:
        # Нормальное поведение: похоже на эталон (+/- 15%)
        return {
            "avgMouseSpeed": baseline["avgMouseSpeed"] * random.uniform(0.85, 1.15),
            "avgKeyPressTime": baseline["avgKeyPressTime"] * random.uniform(0.85, 1.15)
        }
    elif is_anomaly:
        # Аномалия: бот или хакер (очень быстро или очень медленно)
        base_speed = baseline["avgMouseSpeed"] if baseline else 150
        return {
            "avgMouseSpeed": base_speed * random.uniform(2.5, 4.0),  # Слишком быстро
            "avgKeyPressTime": 0.01  # Мгновенные нажатия
        }
    else:
        # Генерация "с нуля" (для нового профиля)
        return {
            "avgMouseSpeed": random.uniform(50, 300),
            "avgKeyPressTime": random.uniform(0.05, 0.35)
        }


def calculate_trust_score(baseline: dict, current: dict) -> float:
    """Сравнивает текущее поведение с эталоном."""
    if not baseline: return 1.0  # Если эталона нет, мы доверяем (режим обучения)

    base_mouse = baseline.get("avgMouseSpeed", 100)
    curr_mouse = current.get("avgMouseSpeed", 100)

    # Расчет отклонения
    diff = abs(base_mouse - curr_mouse) / base_mouse

    # Формула: 1.0 - это полное совпадение. 0.0 - сильное различие.
    score = max(0.0, 1.0 - diff)
    return round(score, 2)


# --- API ---

@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@app.post("/internal/register")
async def register_user(data: dict = Body(...), db: AsyncSession = Depends(get_db)):
    # ТОЛЬКО создание пользователя. Никакого профилирования.
    result = await db.execute(select(User).where(User.login == data['username']))
    if result.scalars().first():
        raise HTTPException(status_code=400, detail="User exists")

    new_user = User(login=data['username'], password=data['password'])
    db.add(new_user)
    await db.commit()
    return {"status": "created"}


@app.post("/internal/login")
async def login_user(data: dict = Body(...), db: AsyncSession = Depends(get_db)):
    # Проверка пароля
    result = await db.execute(select(User).where(User.login == data['username']))
    user = result.scalars().first()

    if not user or user.password != data['password']:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Создание сессии
    session_id = secrets.token_hex(16)
    sessions[session_id] = user.login

    return {"session_id": session_id, "username": user.login}


@app.post("/internal/verify_session")
async def verify_session(data: dict = Body(...), db: AsyncSession = Depends(get_db)):
    """
    ГЛАВНАЯ ЛОГИКА:
    Вызывается основным приложением при КАЖДОМ действии пользователя.
    """
    session_id = data.get("session_id")
    if not session_id or session_id not in sessions:
        raise HTTPException(status_code=401, detail="Invalid session")

    username = sessions[session_id]

    # 1. Получаем пользователя и его профиль
    user_res = await db.execute(select(User).where(User.login == username))
    user = user_res.scalars().first()

    prof_res = await db.execute(select(BehaviorProfile).where(BehaviorProfile.user_id == user.uuid))
    profile = prof_res.scalars().first()

    # 2. СИМУЛЯЦИЯ: Якобы пришли данные от браузера (Mouse, Keyboard)
    # Шанс 20% на аномалию (для теста защиты)
    simulate_attack = (random.random() < 0.2)

    # Если профиль есть, используем его как базу для генерации "похожих" данных (или атаки)
    baseline_features = profile.features if profile else None
    current_behavior = generate_simulation_data(is_anomaly=simulate_attack, baseline=baseline_features)

    decision = True
    trust_score = 1.0
    mode = "UNKNOWN"

    # 3. ЛОГИКА ВЕТВЛЕНИЯ
    if not profile:
        # --- РЕЖИМ ОБУЧЕНИЯ (LEARNING MODE) ---
        # Пользователь новый. Мы просто записываем его поведение как эталон.
        # Блокировки нет.
        mode = "LEARNING"
        new_profile = BehaviorProfile(user_id=user.uuid, features=current_behavior)
        db.add(new_profile)
        print(f"[AUTH] User {username}: First actions -> Profile Created (Learning)")
    else:
        # --- РЕЖИМ ЗАЩИТЫ (PROTECTION MODE) ---
        # Профиль уже есть. Сравниваем текущее поведение с эталоном.
        mode = "PROTECTION"
        trust_score = calculate_trust_score(baseline_features, current_behavior)

        # Порог доверия
        decision = trust_score >= 0.5

        if not decision:
            print(f"[AUTH] ALARM! User {username}: Behavior mismatch! Score: {trust_score}")
        else:
            print(f"[AUTH] User {username}: Behavior OK. Score: {trust_score}")

    # 4. Логируем событие
    db.add(AuthEvent(user_id=user.uuid, score=trust_score, decision=decision))
    await db.commit()

    if not decision:
        del sessions[session_id]  # Кикнуть пользователя
        raise HTTPException(status_code=403, detail="Behavior Verification Failed")

    return {
        "status": "allowed",
        "username": username,
        "mode": mode,
        "score": trust_score
    }


@app.post("/internal/logout")
async def logout(data: dict = Body(...)):
    session_id = data.get("session_id")
    if session_id in sessions:
        del sessions[session_id]
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("auth_service:app", host="0.0.0.0", port=8001, reload=True)