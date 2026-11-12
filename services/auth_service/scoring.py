import math
from typing import List, Dict, Any, Optional, Tuple
import numpy as np
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.ext.asyncio import AsyncSession

from .models import BehaviorProfile

FEATURE_KEYS = [
    "avg_mouse_speed",
    "std_mouse_speed",
    "avg_mouse_accel",
    "avg_key_dwell",
    "avg_key_flight",
]


class FeatureExtractor:
    """
    Класс для извлечения физических/поведенческих биометрических признаков
    из сырых событий мыши и клавиатуры, полученных по WebSocket.
    """

    @staticmethod
    def extract_features(events: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Преобразует сырые события браузера в вектор из 5 биометрических признаков:
          1. avg_mouse_speed (Средняя скорость курсора мыши, пикс/мс)
          2. std_mouse_speed (Стандартное отклонение скорости мыши, пикс/мс)
          3. avg_mouse_accel (Среднее абсолютное ускорение курсора мыши, пикс/мс²)
          4. avg_key_dwell (Среднее время удержания клавиши — Dwell Time, мс)
          5. avg_key_flight (Среднее время перелёта между клавишами — Flight Time, мс)
        """
        if not events:
            return {
                "event_count": 0,
                "click_count": 0,
                "keystrokes_count": 0,
                "has_mouse": False,
                "has_keyboard": False,
                "avg_mouse_speed": 0.0,
                "std_mouse_speed": 0.0,
                "avg_mouse_accel": 0.0,
                "avg_key_dwell": 0.0,
                "avg_key_flight": 0.0,
                "vector": [0.0, 0.0, 0.0, 0.0, 0.0],
            }

        # ---------------------------------------------------------------------
        # 1. Мышь: скорости и ускорения
        # ---------------------------------------------------------------------
        mouse_moves = [e for e in events if e.get("t") == "mm"]
        mouse_moves.sort(key=lambda x: x.get("ts", 0))

        speeds: List[float] = []
        dt_list: List[float] = []

        for i in range(1, len(mouse_moves)):
            p1, p2 = mouse_moves[i - 1], mouse_moves[i]
            dt = p2.get("ts", 0) - p1.get("ts", 0)
            if dt > 0:
                dist = math.hypot(
                    p2.get("x", 0) - p1.get("x", 0),
                    p2.get("y", 0) - p1.get("y", 0)
                )
                v = dist / dt
                speeds.append(v)
                dt_list.append(dt)

        if speeds:
            avg_mouse_speed = sum(speeds) / len(speeds)
        else:
            avg_mouse_speed = 0.0

        if len(speeds) >= 2:
            std_mouse_speed = float(np.std(speeds))
            accels: List[float] = []
            for i in range(1, len(speeds)):
                dt_i = dt_list[i]
                if dt_i > 0:
                    a = abs(speeds[i] - speeds[i - 1]) / dt_i
                    accels.append(a)
            avg_mouse_accel = (sum(accels) / len(accels)) if accels else 0.0
        else:
            std_mouse_speed = 0.0
            avg_mouse_accel = 0.0

        # ---------------------------------------------------------------------
        # 2. Клавиатура: Dwell Time и Flight Time
        # ---------------------------------------------------------------------
        key_events = [e for e in events if e.get("t") in ("kd", "ku")]
        key_events.sort(key=lambda x: x.get("ts", 0))

        dwells: List[float] = []
        flights: List[float] = []

        pending_kd: Dict[str, List[int]] = {}
        last_ku_ts: Optional[int] = None

        for e in key_events:
            ev_type = e.get("t")
            ts = e.get("ts", 0)
            key_name = str(e.get("key", "char"))

            if ev_type == "kd":
                # Flight time: отпускание предыдущей -> нажатие следующей
                if last_ku_ts is not None:
                    flight = ts - last_ku_ts
                    if 0 <= flight < 2000:
                        flights.append(flight)
                    last_ku_ts = None
                pending_kd.setdefault(key_name, []).append(ts)

            elif ev_type == "ku":
                last_ku_ts = ts
                # Dwell time: нажатие -> отпускание
                if key_name in pending_kd and pending_kd[key_name]:
                    kd_ts = pending_kd[key_name].pop(0)
                    dwell = ts - kd_ts
                    if 0 < dwell < 1000:
                        dwells.append(dwell)

        avg_key_dwell = (sum(dwells) / len(dwells)) if dwells else 0.0
        avg_key_flight = (sum(flights) / len(flights)) if flights else 0.0

        clicks = [e for e in events if e.get("t") in ("md", "mu", "mc")]
        key_downs = [e for e in events if e.get("t") == "kd"]

        has_mouse = len(speeds) > 0
        has_keyboard = len(dwells) > 0

        r_avg_speed = round(avg_mouse_speed, 6)
        r_std_speed = round(std_mouse_speed, 6)
        r_avg_accel = round(avg_mouse_accel, 6)
        r_avg_dwell = round(avg_key_dwell, 4)
        r_avg_flight = round(avg_key_flight, 4)

        return {
            # Метаданные (для совместимости с БД и счётчиками):
            "event_count": len(events),
            "click_count": len(clicks),
            "keystrokes_count": len(key_downs),
            "has_mouse": has_mouse,
            "has_keyboard": has_keyboard,
            # 5 биометрических признаков:
            "avg_mouse_speed": r_avg_speed,
            "std_mouse_speed": r_std_speed,
            "avg_mouse_accel": r_avg_accel,
            "avg_key_dwell": r_avg_dwell,
            "avg_key_flight": r_avg_flight,
            "vector": [r_avg_speed, r_std_speed, r_avg_accel, r_avg_dwell, r_avg_flight],
        }


class BehaviorEvaluator:
    """
    Класс для статистической оценки соответствия входящего поведения
    эталонному профилю пользователя (Поведенческая биометрия).
    Реализует 3 математических алгоритма вычисления Trust Score:
      1. interval — Метод интервальной оценки (правило k-сигм)
      2. mahalanobis — Метод на основе расстояния Махаланобиса
      3. cosine_dynamic — Косинусное сходство с динамическим накоплением эталона (EMA)
    """

    ACTIVE_ALGORITHM: str = "interval"

    @classmethod
    def set_active_algorithm(cls, algo: str) -> bool:
        if algo in ("interval", "mahalanobis", "cosine_dynamic"):
            cls.ACTIVE_ALGORITHM = algo
            return True
        return False

    @staticmethod
    def _get_active_indices(features: Dict[str, Any]) -> List[int]:
        """Определяет индексы активных признаков в текущем батче."""
        has_mouse = features.get("has_mouse", False)
        has_keyboard = features.get("has_keyboard", False)
        indices = []
        if has_mouse:
            indices.extend([0, 1, 2])
        if has_keyboard:
            indices.extend([3, 4])
        # Если нет явных меток, но вектор не нулевой
        if not indices and features.get("vector"):
            indices = [i for i, v in enumerate(features["vector"]) if v > 0]
        return indices

    @staticmethod
    def _evaluate_interval(
        vector: List[float],
        active_indices: List[int],
        means: List[float],
        stds: List[float],
        k: float = 3.0
    ) -> float:
        """
        Метод интервальной оценки (правило k-сигм).
        Для каждого признака проверяется отклонение от эталонного среднего |x_j - mu_j|.
        """
        if not active_indices:
            return 1.0

        scores = []
        for j in active_indices:
            x_j = vector[j]
            mu_j = means[j]
            std_j = stds[j]
            # Защита от нулевого std
            sigma_eff = max(std_j, 0.05 * max(abs(mu_j), 0.01), 1e-4)
            z = abs(x_j - mu_j) / sigma_eff

            if z <= k:
                # В пределах доверительного интервала: скор от 1.0 до 0.6
                score_j = 1.0 - 0.4 * (z / k)
            else:
                # За пределами k-сигм: экспоненциальное падение
                score_j = 0.6 * math.exp(-(z - k))

            scores.append(max(0.0, min(1.0, score_j)))

        return sum(scores) / len(scores)

    @staticmethod
    def _evaluate_mahalanobis(
        vector: List[float],
        active_indices: List[int],
        means: List[float],
        cov_matrix: List[List[float]]
    ) -> float:
        """
        Метод на основе расстояния Махаланобиса.
        Учитывает дисперсии и корреляции между биометрическими признаками.
        D_M = sqrt((x - mu)^T * Sigma^(-1) * (x - mu))
        """
        if not active_indices:
            return 1.0

        d = len(active_indices)
        x_sub = np.array([vector[j] for j in active_indices], dtype=np.float64)
        mu_sub = np.array([means[j] for j in active_indices], dtype=np.float64)
        cov_arr = np.array(cov_matrix, dtype=np.float64)
        cov_sub = cov_arr[np.ix_(active_indices, active_indices)]

        # Регуляризация ковариационной матрицы для численной устойчивости обратной матрицы
        cov_reg = cov_sub + np.eye(d) * 1e-4
        try:
            inv_cov = np.linalg.pinv(cov_reg)
            delta = x_sub - mu_sub
            dm_sq = float(np.dot(delta, np.dot(inv_cov, delta)))
            dm_sq = max(0.0, dm_sq)
            dm = math.sqrt(dm_sq)
        except Exception:
            return 0.5

        # Нормированное расстояние Махаланобиса на ожидаемое значение sqrt(d)
        rho = dm / math.sqrt(d)

        if rho <= 1.0:
            score = 1.0 - 0.3 * rho
        else:
            score = 0.7 * math.exp(-0.5 * (rho - 1.0))

        return max(0.0, min(1.0, float(score)))

    @staticmethod
    def _evaluate_cosine_dynamic(
        vector: List[float],
        active_indices: List[int],
        ema_vector: List[float],
        means: List[float],
        stds: List[float]
    ) -> float:
        """
        Косинусное сходство с динамическим накоплением эталона (EMA).
        Признаки масштабируются для выравнивания размерностей (пикс/мс vs мс).
        """
        if not active_indices:
            return 1.0

        x_scaled = []
        ema_scaled = []
        for j in active_indices:
            scale_j = max(stds[j], 0.05 * max(abs(means[j]), 0.01), 1e-4)
            x_scaled.append(vector[j] / scale_j)
            ema_scaled.append(ema_vector[j] / scale_j)

        vx = np.array(x_scaled, dtype=np.float64)
        ve = np.array(ema_scaled, dtype=np.float64)

        norm_x = float(np.linalg.norm(vx))
        norm_e = float(np.linalg.norm(ve))

        if norm_x < 1e-9 or norm_e < 1e-9:
            return 0.5

        cosine_sim = float(np.dot(vx, ve) / (norm_x * norm_e))
        cosine_sim = max(0.0, min(1.0, cosine_sim))

        # Калибровка косинусного сходства в Trust Score
        if cosine_sim >= 0.90:
            score = 0.70 + 0.30 * ((cosine_sim - 0.90) / 0.10)
        else:
            score = 0.70 * ((cosine_sim / 0.90) ** 2)

        return max(0.0, min(1.0, float(score)))

    @classmethod
    async def evaluate_trust(
        cls,
        username: str,
        features: Dict[str, Any],
        current_score: float,
        profile: Optional[BehaviorProfile],
        forced_score: Optional[float] = None,
        algorithm: Optional[str] = None,
        return_all_scores: bool = False
    ) -> Any:
        """
        Сравнивает биометрические признаки `features` с эталонным профилем `profile`.
        Вычисляет Trust Score по 3 алгоритмам и возвращает результат в соответствии с активным алгоритмом.
        """
        algo_name = algorithm or cls.ACTIVE_ALGORITHM

        # Для ручного скриптового тестирования: если передан forced_score
        if forced_score is not None:
            forced = max(0.0, min(1.0, float(forced_score)))
            scores = {
                "interval": forced,
                "mahalanobis": forced,
                "cosine_dynamic": forced,
            }
            if return_all_scores:
                return forced, scores
            return forced

        if not profile or not features or not features.get("vector"):
            scores = {
                "interval": current_score,
                "mahalanobis": current_score,
                "cosine_dynamic": current_score,
            }
            if return_all_scores:
                return current_score, scores
            return current_score

        profile_data = dict(profile.features or {})
        history = profile_data.get("vectors_history", [])
        status = profile_data.get("status", "learning")

        vector = features.get("vector", [0.0] * 5)
        active_indices = cls._get_active_indices(features)

        # Если ещё нет достаточной истории обучения (менее 3 наблюдений) или режим learning
        baseline = profile_data.get("baseline")
        if not baseline or len(history) < 3 or not active_indices:
            scores = {
                "interval": 1.0,
                "mahalanobis": 1.0,
                "cosine_dynamic": 1.0,
            }
            if return_all_scores:
                return 1.0, scores
            return 1.0

        means = baseline.get("means", [0.0] * 5)
        stds = baseline.get("stds", [0.0] * 5)
        cov_matrix = baseline.get("cov", np.eye(5).tolist())
        ema_vector = profile_data.get("ema_vector", means)

        # Вычисление всех 3 статистических алгоритмов
        score_interval = cls._evaluate_interval(vector, active_indices, means, stds)
        score_mahalanobis = cls._evaluate_mahalanobis(vector, active_indices, means, cov_matrix)
        score_cosine = cls._evaluate_cosine_dynamic(vector, active_indices, ema_vector, means, stds)

        all_scores = {
            "interval": round(score_interval, 4),
            "mahalanobis": round(score_mahalanobis, 4),
            "cosine_dynamic": round(score_cosine, 4),
        }

        active_score = all_scores.get(algo_name, all_scores["interval"])

        if return_all_scores:
            return active_score, all_scores
        return active_score


class ProfileEngine:
    """
    Класс для накопления опыта и обновления эталонных математических профилей
    пользователей (Статистический скоринг / Поведенческая биометрия).
    """

    @staticmethod
    async def update_profile(
        username: str,
        features: Dict[str, Any],
        new_score: float,
        db: AsyncSession
    ):
        """
        Обновляет статистический эталон поведения и параметры профиля в PostgreSQL (auth_service_db).
        """
        if not features:
            return

        profile_res = await db.execute(
            select(BehaviorProfile).where(BehaviorProfile.username == username)
        )
        profile = profile_res.scalars().first()

        if not profile:
            return

        features_data = dict(profile.features or {})
        events_collected = features_data.get("events_collected", 0) + features.get("event_count", 0)
        features_data["events_collected"] = events_collected

        if new_score < 0.4:
            features_data["negative_events"] = features_data.get("negative_events", 0) + 1

        if events_collected >= 500:
            features_data["status"] = "protected"
        else:
            features_data.setdefault("status", "learning")

        vec = features.get("vector")
        has_activity = features.get("has_mouse") or features.get("has_keyboard")

        # Обучаем и обновляем эталон только на легитимном поведении
        if vec and has_activity and (features_data.get("status") == "learning" or new_score >= 0.4):
            history = features_data.get("vectors_history", [])
            history.append(vec)
            # Ограничиваем скользящее окно истории 150 векторами
            if len(history) > 150:
                history = history[-150:]
            features_data["vectors_history"] = history

            # Расчёт статистических характеристик
            X = np.array(history, dtype=np.float64)
            means = np.mean(X, axis=0).tolist()
            if len(history) > 1:
                stds = np.std(X, axis=0, ddof=1).tolist()
                cov = np.cov(X, rowvar=False)
                if cov.ndim == 0:
                    cov = np.eye(5) * 0.01
            else:
                stds = [0.05 * max(abs(m), 0.01) for m in means]
                cov = np.eye(5) * 0.01

            features_data["baseline"] = {
                "means": [round(float(m), 6) for m in means],
                "stds": [round(float(s), 6) for s in stds],
                "cov": [[round(float(v), 6) for v in row] for row in cov.tolist()],
            }

            # Обновление динамического EMA вектора
            ema_vector = features_data.get("ema_vector")
            if not ema_vector:
                features_data["ema_vector"] = [round(float(v), 6) for v in vec]
            else:
                alpha = 0.15
                updated_ema = []
                has_mouse = features.get("has_mouse", False)
                has_kb = features.get("has_keyboard", False)
                for j in range(5):
                    is_mouse_feat = (j in (0, 1, 2))
                    if (is_mouse_feat and has_mouse) or (not is_mouse_feat and has_kb):
                        val = alpha * vec[j] + (1 - alpha) * ema_vector[j]
                    else:
                        val = ema_vector[j]
                    updated_ema.append(round(float(val), 6))
                features_data["ema_vector"] = updated_ema

        profile.features = features_data
        flag_modified(profile, "features")
        db.add(profile)
        await db.commit()

