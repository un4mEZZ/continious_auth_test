import os
import sys
import time
import asyncio
import json
from pathlib import Path
from typing import List, Dict, Any, Optional

# Ensure project root is in sys.path
ROOT_DIR = Path(__file__).parent.parent.resolve()
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import websockets
import grpc
from proto import auth_pb2, auth_pb2_grpc

AUTH_GRPC_URL = os.getenv("AUTH_GRPC_URL", "localhost:50051")
WS_URL = os.getenv("WS_URL", "ws://localhost:8001/ws/telemetry/")


def generate_bot_events(base_ts: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Генерирует пакет нелегитимных "ботовидных" телеметрических событий:
      - Сверхчеловеческая скорость перемещения курсора мыши (~900+ пикс/мс).
      - Нулевые физиологические задержки клавиатуры (Dwell = 1 мс, Flight = 1 мс).
      - Неестественные скачки координат без плавного разгона и торможения руки.
    Данные признаки приводят к резкому падению Trust Score во всех 3 статистических алгоритмах.
    """
    if base_ts is None:
        base_ts = int(time.time() * 1000)

    events = [
        # Сверхбыстрые неестественные перемещения мыши (телепортация)
        {"t": "mm", "ts": base_ts, "x": 10, "y": 10},
        {"t": "mm", "ts": base_ts + 2, "x": 1600, "y": 900},
        {"t": "mm", "ts": base_ts + 4, "x": 50, "y": 40},
        {"t": "mm", "ts": base_ts + 6, "x": 1920, "y": 1080},
        # Машинный ввод с клавиатуры без физиологических задержек
        {"t": "kd", "ts": base_ts + 20, "key": "char"},
        {"t": "ku", "ts": base_ts + 21, "key": "char"},  # dwell = 1 ms
        {"t": "kd", "ts": base_ts + 22, "key": "char"},  # flight = 1 ms
        {"t": "ku", "ts": base_ts + 23, "key": "char"},  # dwell = 1 ms
        {"t": "kd", "ts": base_ts + 24, "key": "char"},  # flight = 1 ms
        {"t": "ku", "ts": base_ts + 25, "key": "char"},  # dwell = 1 ms
        # Мгновенный клик
        {"t": "md", "ts": base_ts + 30, "x": 1920, "y": 1080, "btn": 0},
        {"t": "mu", "ts": base_ts + 31, "x": 1920, "y": 1080, "btn": 0},
    ]
    return events


async def run_bot_test(username: str = "alice", client_ip: str = "127.0.0.1"):
    """
    Сценарий тестирования защиты от ботов:
    1. Создание сессии через gRPC.
    2. Отправка пакета нелегитимной телеметрии (ботовидного поведения).
    3. Фиксация падения Trust Score и закрытия WS-соединения сервером (код 4001/4003).
    """
    print("================================================================")
    print("  БОТ-ТЕСТ: СИМУЛЯЦИЯ НЕЛЕГИТИМНОГО БОТОВИДНОГО ПОВЕДЕНИЯ")
    print(f"  Целевой пользователь: '{username}' | IP: {client_ip}")
    print("================================================================\n")

    channel = grpc.aio.insecure_channel(AUTH_GRPC_URL)
    stub = auth_pb2_grpc.AuthServiceStub(channel)

    # 1. Создание сессии
    print(f"[1] Создание сессии для пользователя '{username}'...")
    create_resp = await stub.CreateSession(
        auth_pb2.CreateSessionRequest(username=username, client_ip=client_ip)
    )
    if not create_resp.success:
        print(f"Ошибка создания сессии: {create_resp.message}")
        await channel.close()
        return

    session_id = create_resp.session_id
    print(f"    Сессия создана: {session_id}")

    # Проверка исходного статуса
    verify_resp = await stub.VerifySession(
        auth_pb2.VerifySessionRequest(session_id=session_id, client_ip=client_ip)
    )
    print(f"    Начальный статус: {auth_pb2.SessionStatus.Name(verify_resp.status)} (Trust Score: {verify_resp.trust_score})\n")

    # 2. Отправка пакета нелегитимной телеметрии бота
    print("[2] Отправка батча ботовидных биометрических событий по WebSocket...")
    ws_endpoint = f"{WS_URL}{session_id}"
    bot_events = generate_bot_events()

    payload = {
        "sequence_id": 1,
        "batch_timestamp": int(time.time() * 1000),
        "events": bot_events
    }

    try:
        async with websockets.connect(ws_endpoint) as ws:
            await ws.send(json.dumps(payload))
            msg = await ws.recv()
            print(f"    Ответ WebSocket: {msg}")
    except websockets.exceptions.ConnectionClosed as e:
        print(f"    -> WS закрыт сервером! Код: {e.code}, Причина: '{e.reason}'")
        if e.code == 4001:
            print("    УСПЕХ: Сервер зафиксировал аномалию и потребовал повторную аутентификацию (4001)!")
        elif e.code == 4003:
            print("    УСПЕХ: IP заблокирован из-за нарушений (4003)!")
        else:
            print(f"    Код закрытия: {e.code}")

    # 3. Проверка статуса сессии через gRPC после атаки
    verify_resp = await stub.VerifySession(
        auth_pb2.VerifySessionRequest(session_id=session_id, client_ip=client_ip)
    )
    print(f"\n[3] Итоговый статус сессии: {auth_pb2.SessionStatus.Name(verify_resp.status)}")
    print(f"    Trust Score после теста бота: {verify_resp.trust_score:.4f}")
    print(f"    Сообщение системы: '{verify_resp.message}'")

    await channel.close()
    print("\n================================================================")
    print("  ТЕСТИРОВАНИЕ БОТОВИДНОГО ПОВЕДЕНИЯ ЗАВЕРШЕНО")
    print("================================================================")


if __name__ == "__main__":
    target_user = sys.argv[1] if len(sys.argv) > 1 else "alice"
    asyncio.run(run_bot_test(target_user))

