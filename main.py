"""
Telegram Bot для напоминаний о приёме лекарств с AI-ассистентом
Version: 3.0 (FastAPI + AI Integration)
Platform: Bothost.ru
"""



from dotenv import load_dotenv
import os
import logging
import asyncio
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, date
from typing import Dict, List, Optional, Tuple, Set
from pathlib import Path

from fastapi import FastAPI, Request, Response
from aiogram import Bot, Dispatcher, Router, F
from aiogram import types
from aiogram.filters import Command
from aiogram.types import (
    Message, 
    ReplyKeyboardMarkup, 
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
    CallbackQuery
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from redis.asyncio import Redis
from filelock import FileLock
import aiohttp

load_dotenv()

# === НАСТРОЙКА ЛОГИРОВАНИЯ ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# === КОНФИГУРАЦИЯ ===
class Config:
    """Настройки бота"""
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    MANAGER_ID = int(os.getenv("MANAGER_ID", "834868627"))
    TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
    
    # OpenRouter AI
    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
    AI_TEXT_MODEL = "qwen/qwen-3-coder-480b-instruct:free"
    AI_IMAGE_MODEL = "google/gemini-2.5-flash-image-preview:free"
    
    # Redis
    REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
    REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
    REDIS_DB = int(os.getenv("REDIS_DB", "0"))
    
    # Features
    ENABLE_AI_FEATURES = os.getenv("ENABLE_AI_FEATURES", "true").lower() == "true"
    ENABLE_MORNING_MOTIVATION = os.getenv("ENABLE_MORNING_MOTIVATION", "true").lower() == "true"
    USE_POLLING = os.getenv("USE_POLLING", "false").lower() == "true"
    
    # Rate Limits
    AI_DAILY_LIMIT_ASK = int(os.getenv("AI_DAILY_LIMIT_ASK", "5"))
    AI_DAILY_LIMIT_IMAGE = int(os.getenv("AI_DAILY_LIMIT_IMAGE", "3"))
    
    # Scheduler
    MORNING_MOTIVATION_TIME = "08:00"
    REPORT_DAY = 0  # Понедельник
    REPORT_HOUR = 9
    REPORT_MINUTE = 0
    SOS_ALERT_HOURS = 2

config = Config()


# === ВАЛИДАЦИЯ ТОКЕНА ===
_BOT_TOKEN_PATTERN = re.compile(r"^[0-9]{6,}:[A-Za-z0-9_-]{20,}$")

def _validate_bot_token(token: str) -> None:
    """Проверка токена до старта (чтобы ошибка была понятнее)."""
    if not token:
        raise RuntimeError("BOT_TOKEN пустой. Создайте .env рядом с main.py и задайте BOT_TOKEN=...")
    if not _BOT_TOKEN_PATTERN.match(token.strip()):
        raise RuntimeError("BOT_TOKEN выглядит некорректно. Проверьте токен от @BotFather (формат digits:... ).")

# === КОНСТАНТЫ ===
DATA_DIR = Path("data")
DB_FILE = DATA_DIR / "medications.json"
DB_LOCK_FILE = DATA_DIR / "medications.json.lock"

WEEKDAY_MAP = {
    0: "пн", 1: "вт", 2: "ср", 3: "чт",
    4: "пт", 5: "сб", 6: "вс"
}

# Временное хранилище для inline-кнопок (fallback если Redis недоступен)
user_temp_data: Dict[int, Dict] = {}

# === FSM СОСТОЯНИЯ ===
class OnboardingStates(StatesGroup):
    """Онбординг новых пользователей"""
    waiting_name = State()
    waiting_age = State()

class MedicineStates(StatesGroup):
    """Добавление/удаление препаратов"""
    waiting_medicine_name = State()
    waiting_times = State()
    waiting_frequency = State()
    waiting_delete_confirmation = State()
    waiting_time_custom = State()

# === БАЗА ДАННЫХ ===
class MedicineDB:
    """
    Управление базой данных препаратов (JSON с filelock)
    """
    def __init__(self, file_path: Path = DB_FILE):
        self.file = file_path
        self.lock_file = DB_LOCK_FILE
        self.data: Dict = {}
        self._ensure_directory()
        self._load_data()
    
    def _ensure_directory(self) -> None:
        """Создать директорию для данных"""
        self.file.parent.mkdir(parents=True, exist_ok=True)
    
    def _load_data(self) -> None:
        """Загрузить данные из файла"""
        if self.file.exists():
            try:
                with open(self.file, 'r', encoding='utf-8') as f:
                    self.data = json.load(f)
                logger.info(f"✅ Загружено пользователей: {len(self.data)}")
            except json.JSONDecodeError as e:
                logger.error(f"❌ Ошибка чтения JSON: {e}")
                self.data = {}
        else:
            self.data = {}
            logger.info("📝 Создана новая БД")
    
    def _save_data(self) -> None:
        """Сохранить данные в файл с блокировкой"""
        try:
            self._ensure_directory()
            lock = FileLock(self.lock_file, timeout=5)
            with lock:
                with open(self.file, 'w', encoding='utf-8') as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения: {e}")
    
    def reload(self) -> None:
        """Перезагрузить данные из файла"""
        self._load_data()
    
    # === USER MANAGEMENT ===
    def get_user(self, user_id: int) -> Optional[Dict]:
        """Получить данные пользователя"""
        user_key = str(user_id)
        if user_key not in self.data:
            return None
        
        # Если старый формат (без user_info) - создаём новый
        if "user_info" not in self.data[user_key]:
            return {
                "user_id": user_id,
                "name": None,
                "age": None,
                "onboarding_completed": False,
                "created_at": datetime.now().isoformat(),
                "medications": self.data[user_key]
            }
        
        return self.data[user_key]["user_info"]
    
    def create_user(self, user_id: int, name: str, age: int) -> None:
        """Создать или обновить пользователя"""
        user_key = str(user_id)
        
        # Сохраняем существующие препараты если есть
        existing_meds = {}
        if user_key in self.data and isinstance(self.data[user_key], dict):
            # Старый формат - извлекаем препараты
            existing_meds = {k: v for k, v in self.data[user_key].items() 
                           if k != "user_info"}
        
        # Новый формат с разделением user_info и medications
        self.data[user_key] = {
            "user_info": {
                "user_id": user_id,
                "name": name,
                "age": age,
                "onboarding_completed": True,
                "created_at": datetime.now().isoformat(),
                "streak": 0,
                "achievements": []
            },
            "medications": existing_meds
        }
        self._save_data()
        logger.info(f"✅ Создан пользователь {name} ({age} лет)")
    
    # === MEDICATION MANAGEMENT ===
    def add_medication(
        self, 
        user_id: int, 
        med_name: str, 
        times: List[str], 
        frequency: str,
        ai_data: Optional[Dict] = None
    ) -> None:
        """Добавить препарат"""
        user_key = str(user_id)
        
        if user_key not in self.data:
            self.data[user_key] = {"medications": {}}
        
        if "medications" not in self.data[user_key]:
            self.data[user_key]["medications"] = {}
        
        med_data = {
            'times': times,
            'frequency': frequency,
            'added_at': datetime.now().isoformat(),
            'history': {}
        }
        
        # Добавляем AI-рекомендации если есть
        if ai_data:
            med_data.update({
                'ai_compatibility': ai_data.get('compatibility'),
                'ai_interactions': ai_data.get('interactions'),
                'best_time': ai_data.get('best_time'),
                'food_timing': ai_data.get('food_timing'),
                'food_explanation': ai_data.get('food_explanation'),
                'side_effects': ai_data.get('side_effects', []),
                'ai_recommendation': ai_data.get('recommendation')
            })
        
        self.data[user_key]["medications"][med_name] = med_data
        self._save_data()
        logger.info(f"✅ Добавлен препарат '{med_name}' для user {user_id}")
    
    def get_medications(self, user_id: int) -> Dict:
        """Получить все препараты пользователя"""
        user_key = str(user_id)
        if user_key not in self.data:
            return {}
        
        # Поддержка старого формата
        if "medications" in self.data[user_key]:
            return self.data[user_key]["medications"]
        else:
            # Старый формат - всё кроме user_info это препараты
            return {k: v for k, v in self.data[user_key].items() 
                   if k != "user_info"}
    
    def delete_medication(self, user_id: int, med_name: str) -> bool:
        """Удалить препарат"""
        user_key = str(user_id)
        medications = self.get_medications(user_id)
        
        if med_name not in medications:
            return False
        
        if "medications" in self.data[user_key]:
            del self.data[user_key]["medications"][med_name]
        else:
            del self.data[user_key][med_name]
        
        self._save_data()
        logger.info(f"🗑️ Удалён препарат '{med_name}' для user {user_id}")
        return True
    
    def mark_taken(self, user_id: int, med_name: str, time: str) -> bool:
        """Отметить приём препарата"""
        medications = self.get_medications(user_id)
        
        if med_name not in medications:
            return False
        
        today = datetime.now().strftime("%Y-%m-%d")
        
        if today not in medications[med_name]['history']:
            medications[med_name]['history'][today] = {}
        
        medications[med_name]['history'][today][time] = True
        self._save_data()
        logger.info(f"✅ Отмечен приём {med_name} в {time} для user {user_id}")
        return True
    
    def get_week_report(self, user_id: int) -> str:
        """Сформировать отчёт за неделю"""
        medications = self.get_medications(user_id)
        
        if not medications:
            return "❌ Нет зарегистрированных препаратов"
        
        report_lines = ["📊 ОТЧЁТ ЗА НЕДЕЛЮ\n"]
        
        for med_name, med_data in medications.items():
            report_lines.append(f"💊 {med_name}")
            total_count = 0
            taken_count = 0
            
            for i in range(7):
                date_str = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                day_abbr = WEEKDAY_MAP[(datetime.now() - timedelta(days=i)).weekday()]
                
                if date_str in med_data.get('history', {}):
                    day_taken = sum(1 for v in med_data['history'][date_str].values() if v)
                    day_total = len(med_data['history'][date_str])
                    emoji = "✅" if day_taken == day_total else "⚠️"
                    report_lines.append(f"  {emoji} {day_abbr}: {day_taken}/{day_total}")
                    total_count += day_total
                    taken_count += day_taken
                else:
                    report_lines.append(f"  ⚪ {day_abbr}: -")
            
            if total_count > 0:
                percent = int((taken_count / total_count) * 100)
                report_lines.append(f"  📈 Прилежность: {percent}%")
            
            report_lines.append("")
        
        return "\n".join(report_lines)
    
    def get_missed_reminders(self, user_id: int) -> List[Tuple[str, str, datetime]]:
        """Получить список пропущенных напоминаний"""
        medications = self.get_medications(user_id)
        missed = []
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        
        for med_name, med_data in medications.items():
            for reminder_time in med_data['times']:
                try:
                    hour, minute = map(int, reminder_time.split(':'))
                    reminder_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    time_diff = (now - reminder_dt).total_seconds()
                    
                    if time_diff > config.SOS_ALERT_HOURS * 3600:
                        is_taken = False
                        if today in med_data.get('history', {}):
                            if reminder_time in med_data['history'][today]:
                                is_taken = med_data['history'][today][reminder_time]
                        
                        if not is_taken:
                            missed.append((med_name, reminder_time, reminder_dt))
                except ValueError as e:
                    logger.error(f"❌ Ошибка парсинга времени {reminder_time}: {e}")
        
        return missed
    
    def get_all_users(self) -> List[Dict]:
        """Получить всех пользователей"""
        users = []
        for user_id_str in self.data.keys():
            user = self.get_user(int(user_id_str))
            if user:
                users.append(user)
        return users

# Глобальный экземпляр БД
db = MedicineDB()

# === AI INTEGRATION ===
class OpenRouterClient:
    """Клиент для OpenRouter API"""
    
    def __init__(self):
        self.api_key = config.OPENROUTER_API_KEY
        self.base_url = "https://openrouter.ai/api/v1"
    
    async def complete(
        self, 
        prompt: str, 
        model: Optional[str] = None,
        response_format: Optional[str] = None,
        max_tokens: int = 1000
    ) -> str:
        """Текстовая генерация"""
        if not self.api_key:
            raise ValueError("OpenRouter API key not configured")
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": model or config.AI_TEXT_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens
        }
        
        if response_format:
            payload["response_format"] = {"type": response_format}
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(f"OpenRouter API error: {error_text}")
                        raise Exception(f"API error: {resp.status}")
                    
                    data = await resp.json()
                    return data['choices'][0]['message']['content']
        except Exception as e:
            logger.error(f"❌ OpenRouter error: {e}")
            raise
    
    async def generate_image(self, prompt: str) -> bytes:
        """Генерация изображения"""
        if not self.api_key:
            raise ValueError("OpenRouter API key not configured")
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": config.AI_IMAGE_MODEL,
            "prompt": prompt,
            "n": 1
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/images/generations",
                    headers=headers,
                    json=payload
                ) as resp:
                    if resp.status != 200:
                        raise Exception(f"Image API error: {resp.status}")
                    
                    data = await resp.json()
                    image_url = data['data'][0]['url']
                    
                    # Скачиваем изображение
                    async with session.get(image_url) as img_resp:
                        return await img_resp.read()
        except Exception as e:
            logger.error(f"❌ Image generation error: {e}")
            raise

ai_client = OpenRouterClient() if config.ENABLE_AI_FEATURES else None

# === AI FUNCTIONS ===
async def check_drug_interactions(
    new_drug: str, 
    existing_drugs: List[str], 
    user_age: int
) -> Optional[Dict]:
    """AI-анализ взаимодействий препаратов"""
    if not ai_client:
        return None
    
    try:
        prompt = f"""Ты фармацевт-консультант. Проанализируй добавление препарата.

НОВЫЙ ПРЕПАРАТ: {new_drug}
ТЕКУЩИЕ ПРЕПАРАТЫ: {', '.join(existing_drugs) if existing_drugs else 'нет'}
ВОЗРАСТ ПАЦИЕНТА: {user_age} лет

Проверь:
1. Совместимость с текущими препаратами (опасные взаимодействия?)
2. Оптимальное время приёма (утро/день/вечер)
3. Приём относительно еды (до/после/во время)
4. Основные побочные эффекты (2-3 пункта)

Формат ответа (только JSON):
{{
  "compatibility": "safe|warning|danger",
  "interactions": "описание или 'нет опасных взаимодействий'",
  "best_time": "утро|день|вечер|любое время",
  "food_timing": "до еды|после еды|во время еды|не важно",
  "food_explanation": "краткое объяснение",
  "side_effects": ["побочка 1", "побочка 2"],
  "recommendation": "рекомендация в 1-2 предложениях"
}}"""
        
        response = await ai_client.complete(prompt, response_format="json_object")
        return json.loads(response)
    except Exception as e:
        logger.error(f"AI interaction check failed: {e}")
        return None

async def generate_morning_motivation_text(user_name: str, streak: int, adherence: int) -> str:
    """Генерация мотивационного текста"""
    if not ai_client:
        return f"☀️ Доброе утро, {user_name}! Не забудь про лекарства сегодня!"
    
    try:
        prompt = f"""Создай короткое мотивационное утреннее сообщение.

Контекст:
- Имя: {user_name}
- Серия без пропусков: {streak} дней
- Прилежность за неделю: {adherence}%

Требования:
- 2-3 предложения
- Дружелюбный тон, на "ты"
- Упомянуть достижение (streak) если >= 3 дней
- Мотивировать продолжать
- 2-3 эмодзи
- НЕ использовать штампы типа "хорошего дня"

Пример: "{user_name}, уже {streak} дней подряд! Ты молодец 🔥"
"""
        
        return await ai_client.complete(prompt, max_tokens=150)
    except Exception as e:
        logger.error(f"Morning text generation failed: {e}")
        return f"☀️ Доброе утро, {user_name}! Продолжай в том же духе!"

# === RATE LIMITING ===
async def check_ai_limit(redis_client, user_id: int, limit_type: str = "ask") -> Tuple[bool, int]:
    """Проверка лимита AI-запросов"""
    if not redis_client:
        return True, 999  # Без Redis - безлимитно
    
    limits = {
        "ask": config.AI_DAILY_LIMIT_ASK,
        "image": config.AI_DAILY_LIMIT_IMAGE
    }
    
    key = f"ai_limit:{limit_type}:{user_id}:{date.today()}"
    
    try:
        current = await redis_client.get(key)
        
        if current is None:
            await redis_client.setex(key, 86400, "1")
            return True, limits[limit_type] - 1
        
        current_int = int(current)
        if current_int >= limits[limit_type]:
            return False, 0
        
        await redis_client.incr(key)
        return True, limits[limit_type] - current_int - 1
    except Exception as e:
        logger.error(f"Rate limit check failed: {e}")
        return True, 999  # В случае ошибки - разрешаем

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
TIME_PATTERN = re.compile(r"^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$")

def validate_time(time_str: str) -> bool:
    """Валидация времени"""
    return bool(TIME_PATTERN.match(time_str.strip()))

def parse_time(time_str: str) -> Optional[str]:
    """Парсинг времени в формат HH:MM"""
    time_str = time_str.strip()
    
    if ':' not in time_str:
        try:
            hour = int(time_str)
            if 0 <= hour < 24:
                return f"{hour:02d}:00"
        except ValueError:
            return None
    
    try:
        parts = time_str.split(':')
        if len(parts) == 2:
            hour, minute = int(parts[0]), int(parts[1])
            if 0 <= hour < 24 and 0 <= minute < 60:
                return f"{hour:02d}:{minute:02d}"
    except ValueError:
        pass
    
    return None

def parse_times(times_str: str) -> Optional[List[str]]:
    """Парсинг списка времён"""
    times: List[str] = []
    for t in times_str.split(','):
        ts = t.strip()
        if not validate_time(ts):
            return None
        h, m = ts.split(':')
        times.append(f"{int(h):02d}:{int(m):02d}")
    return times

def should_remind_today(frequency: str, current_day: str) -> bool:
    """Проверить, нужно ли напоминание сегодня"""
    freq_lower = frequency.lower()
    
    if any(word in freq_lower for word in ["каждый день", "ежедневно", "каждый", "daily"]):
        return True
    
    if "через день" in freq_lower or "alt" in freq_lower:
        return datetime.now().day % 2 == 0
    
    if current_day in freq_lower:
        return True
    
    # Рабочие дни
    if ("будни" in freq_lower or "work" in freq_lower) and current_day in ["пн", "вт", "ср", "чт", "пт"]:
        return True
    
    # Выходные
    if ("выходные" in freq_lower or "weekend" in freq_lower) and current_day in ["сб", "вс"]:
        return True
    
    return False

def frequency_label(key: str, selected_days: Optional[Set[str]] = None) -> str:
    """Преобразование ключа частоты в текст"""
    if key == "freq_daily":
        return "каждый день"
    if key == "freq_alt":
        return "через день"
    if key == "freq_work":
        return "пн,вт,ср,чт,пт"
    if key == "freq_weekend":
        return "сб,вс"
    if key == "freq_select" and selected_days:
        human = {"mon": "пн", "tue": "вт", "wed": "ср", "thu": "чт", 
                "fri": "пт", "sat": "сб", "sun": "вс"}
        return ",".join(human[d] for d in selected_days)
    return "каждый день"

def reset_temp_state(user_id: int) -> None:
    """Сброс временного состояния"""
    user_temp_data[user_id] = {}

def ensure_temp_state(user_id: int) -> Dict:
    """Обеспечить наличие временного состояния"""
    if user_id not in user_temp_data:
        reset_temp_state(user_id)
    return user_temp_data[user_id]

# === КЛАВИАТУРЫ ===
def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Главная клавиатура"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить препарат")],
            [KeyboardButton(text="📋 Мой список"), KeyboardButton(text="📊 Отчёт")],
            [KeyboardButton(text="🗑️ Удалить препарат"), KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True
    )

def get_medication_buttons(medications: Dict) -> ReplyKeyboardMarkup:
    """Клавиатура со списком препаратов для удаления"""
    buttons = [[KeyboardButton(text=f"🗑️ {name}")] for name in medications.keys()]
    buttons.append([KeyboardButton(text="↩️ Отмена")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def get_taken_button(med_name: str) -> ReplyKeyboardMarkup:
    """Кнопка 'Принял препарат'"""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=f"✅ ПРИНЯЛ {med_name}")]],
        resize_keyboard=True
    )

def get_time_keyboard() -> InlineKeyboardMarkup:
    """Inline-клавиатура выбора времени"""
    buttons = [
        [
            InlineKeyboardButton(text="🌅 09:00", callback_data="time_0900"),
            InlineKeyboardButton(text="☀️ 12:00", callback_data="time_1200"),
        ],
        [
            InlineKeyboardButton(text="🌆 18:00", callback_data="time_1800"),
            InlineKeyboardButton(text="🌙 21:00", callback_data="time_2100"),
        ],
        [InlineKeyboardButton(text="🕐 Утро+Вечер (09:00, 21:00)", callback_data="time_0900,2100")],
        [InlineKeyboardButton(text="✏️ Ввести своё время", callback_data="time_custom")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_frequency_keyboard() -> InlineKeyboardMarkup:
    """Inline-клавиатура выбора частоты"""
    buttons = [
        [InlineKeyboardButton(text="📅 Каждый день", callback_data="freq_daily")],
        [InlineKeyboardButton(text="📆 Через день", callback_data="freq_alt")],
        [InlineKeyboardButton(text="🏢 Будни (пн-пт)", callback_data="freq_work")],
        [InlineKeyboardButton(text="🏡 Выходные (сб-вс)", callback_data="freq_weekend")],
        [InlineKeyboardButton(text="📆 Выбрать дни недели", callback_data="freq_select")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_weekday_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Inline-клавиатура выбора дней недели"""
    selected: Set[str] = user_temp_data.get(user_id, {}).get("selected_days", set())
    
    days = [
        ("mon", "Понедельник"), ("tue", "Вторник"), ("wed", "Среда"),
        ("thu", "Четверг"), ("fri", "Пятница"), ("sat", "Суббота"), ("sun", "Воскресенье"),
    ]
    
    rows = []
    for code, name in days:
        check = "✅ " if code in selected else ""
        rows.append([InlineKeyboardButton(text=f"{check}{name}", callback_data=f"day_{code}")])
    
    if selected:
        rows.append([InlineKeyboardButton(text="✅ Готово", callback_data="save_days")])
    
    return InlineKeyboardMarkup(inline_keyboard=rows)

# === ГЛОБАЛЬНЫЕ ОБЪЕКТЫ ===
router = Router()
bot = None
dp = None
scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)
redis_client = None

# === ОБРАБОТЧИКИ КОМАНД ===

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """Команда /start - приветствие или онбординг"""
    user = db.get_user(message.from_user.id)
    
    if user and user.get("onboarding_completed"):
        # Пользователь уже зарегистрирован
        await message.answer(
            f"С возвращением, {user['name']}! 👋\n\n"
            "Что будем делать?",
            reply_markup=get_main_keyboard()
        )
        return
    
    # Новый пользователь - начинаем онбординг
    await message.answer(
        "👋 Привет! Я твой персональный помощник по лекарствам!\n\n"
        "Помогу не забывать принимать препараты и позабочусь о твоём здоровье 💊\n\n"
        "Давай познакомимся! Как к тебе обращаться?"
    )
    await state.set_state(OnboardingStates.waiting_name)

@router.message(OnboardingStates.waiting_name)
async def handle_onboarding_name(message: Message, state: FSMContext):
    """Получение имени при онбординге"""
    name = message.text.strip()
    
    if len(name) < 2 or len(name) > 30:
        await message.answer("Имя должно быть от 2 до 30 символов. Попробуй ещё раз:")
        return
    
    await state.update_data(name=name)
    await message.answer(
        f"Приятно познакомиться, {name}! 😊\n\n"
        "Сколько тебе лет? (Это поможет давать более точные рекомендации)"
    )
    await state.set_state(OnboardingStates.waiting_age)

@router.message(OnboardingStates.waiting_age)
async def handle_onboarding_age(message: Message, state: FSMContext):
    """Получение возраста при онбординге"""
    try:
        age = int(message.text.strip())
        if age < 1 or age > 120:
            raise ValueError
    except ValueError:
        await message.answer("Пожалуйста, введи корректный возраст (от 1 до 120):")
        return
    
    data = await state.get_data()
    name = data["name"]
    
    # Сохраняем пользователя
    db.create_user(
        user_id=message.from_user.id,
        name=name,
        age=age
    )
    
    await message.answer(
        f"Отлично! Теперь я готов помогать тебе 🎉\n\n"
        "Нажми \"➕ Добавить лекарство\", чтобы начать!",
        reply_markup=get_main_keyboard()
    )
    await state.clear()

@router.message(Command("add"))
async def cmd_add_quick(message: Message):
    """Быстрое добавление препарата через команду"""
    parts = message.text.split(maxsplit=3)
    
    if len(parts) < 4:
        await message.answer(
            "Формат: /add Название 09:00,21:00 daily|alt|work|weekend|пн,вт\n\n"
            "Пример: /add Глицин 09:00,21:00 daily"
        )
        return
    
    _, med_name, times_raw, freq_raw = parts
    parsed = parse_times(times_raw)
    
    if not parsed:
        await message.answer("❌ Время неверное. Пример: 09:00,21:00")
        return
    
    # Определяем частоту
    freq_key = freq_raw.lower()
    if freq_key in ["daily", "каждый", "каждыйдень"]:
        freq = frequency_label("freq_daily")
    elif freq_key in ["alt", "через", "черездень"]:
        freq = frequency_label("freq_alt")
    elif freq_key in ["work", "workdays", "будни"]:
        freq = frequency_label("freq_work")
    elif freq_key in ["weekend", "сбвс", "выходные"]:
        freq = frequency_label("freq_weekend")
    else:
        freq = freq_key
    
    # Проверяем AI-взаимодействия если включено
    ai_data = None
    if config.ENABLE_AI_FEATURES:
        user = db.get_user(message.from_user.id)
        if user and user.get("age"):
            existing_drugs = list(db.get_medications(message.from_user.id).keys())
            ai_data = await check_drug_interactions(med_name, existing_drugs, user['age'])
    
    db.reload()
    db.add_medication(message.from_user.id, med_name, parsed, freq, ai_data)
    
    response_text = f"✅ Добавлено\n💊 {med_name}\n🕐 {', '.join(parsed)}\n📅 {freq}"
    
    # Добавляем AI-рекомендации если есть
    if ai_data:
        response_text += f"\n\n🤖 AI-РЕКОМЕНДАЦИИ:\n"
        response_text += f"⏰ Лучшее время: {ai_data.get('best_time')}\n"
        response_text += f"🍽️ Приём: {ai_data.get('food_timing')}\n"
        
        if ai_data.get('compatibility') == 'warning':
            response_text += f"\n⚠️ {ai_data.get('interactions')}"
        elif ai_data.get('compatibility') == 'danger':
            response_text += f"\n🚨 ВНИМАНИЕ: {ai_data.get('interactions')}"
    
    await message.answer(response_text, reply_markup=get_main_keyboard())

@router.message(Command("delete"))
async def cmd_delete_quick(message: Message):
    """Быстрое удаление препарата через команду"""
    parts = message.text.split(maxsplit=1)
    
    if len(parts) < 2:
        await message.answer("Формат: /delete Название\n\nПример: /delete Глицин")
        return
    
    med_name = parts[1].strip()
    db.reload()
    ok = db.delete_medication(message.from_user.id, med_name)
    
    if ok:
        await message.answer(f"🗑️ Удалён {med_name}", reply_markup=get_main_keyboard())
        
        try:
            await bot.send_message(
                config.MANAGER_ID,
                f"🗑️ Удалён препарат: {med_name}\n👤 User: {message.from_user.id}"
            )
        except Exception as e:
            logger.error(f"Manager notification failed: {e}")
    else:
        await message.answer("❌ Препарат не найден", reply_markup=get_main_keyboard())

@router.message(Command("ask"))
async def cmd_ask_ai(message: Message):
    """AI-консультант по лекарствам"""
    if not config.ENABLE_AI_FEATURES or not ai_client:
        await message.answer("❌ AI-функции отключены")
        return
    
    # Проверяем лимит
    allowed, remaining = await check_ai_limit(redis_client, message.from_user.id, "ask")
    if not allowed:
        await message.answer(
            "❌ Исчерпан лимит AI-запросов на сегодня (5/день)\n\n"
            "Попробуй завтра!"
        )
        return
    
    # Извлекаем вопрос
    question = message.text.replace("/ask", "").strip()
    if not question:
        await message.answer(
            "Используй формат: /ask Ваш вопрос\n\n"
            "Например: /ask Можно ли принимать глицин с кофе?"
        )
        return
    
    await message.answer("⏳ Ищу информацию...")
    
    # Получаем контекст пользователя
    user = db.get_user(message.from_user.id)
    medications = db.get_medications(message.from_user.id)
    
    prompt = f"""Ты медицинский консультант. Ответь на вопрос.

ВОПРОС: {question}

КОНТЕКСТ ПОЛЬЗОВАТЕЛЯ:
- Возраст: {user.get('age', 'не указан')} лет
- Принимаемые препараты: {', '.join(medications.keys()) if medications else 'нет'}

Дай краткий, понятный ответ (3-4 предложения).
Используй эмодзи для наглядности.
В конце добавь: "⚠️ Это общая информация. При сомнениях — консультируйся с врачом."
"""
    
    try:
        response = await ai_client.complete(prompt, max_tokens=300)
        await message.answer(
            f"🤖 AI-КОНСУЛЬТАНТ:\n\n{response}\n\n"
            f"Осталось запросов сегодня: {remaining}"
        )
    except Exception as e:
        logger.error(f"AI ask failed: {e}")
        await message.answer("❌ Не удалось получить ответ от AI. Попробуй позже.")

# === ОБРАБОТЧИКИ КНОПОК ГЛАВНОГО МЕНЮ ===

@router.message(F.text == "➕ Добавить препарат")
async def handle_add_medicine_button(message: Message, state: FSMContext):
    """Начало добавления препарата через кнопку"""
    reset_temp_state(message.from_user.id)
    
    await message.answer(
        "Как называется препарат?\n\n"
        "Например: Глицин, Аспирин, Ибупрофен",
        reply_markup=types.ReplyKeyboardRemove()
    )
    await state.set_state(MedicineStates.waiting_medicine_name)

@router.message(MedicineStates.waiting_medicine_name)
async def handle_medicine_name(message: Message, state: FSMContext):
    """Получение названия препарата"""
    med_name = message.text.strip()
    
    if len(med_name) < 2 or len(med_name) > 50:
        await message.answer("❌ Название должно быть от 2 до 50 символов. Попробуй ещё раз:")
        return
    
    await state.update_data(med_name=med_name)
    temp_state = ensure_temp_state(message.from_user.id)
    temp_state["med_name"] = med_name
    temp_state["step"] = "await_time"
    
    await message.answer(
        f"✅ {med_name}\n\n"
        "Выбери время приёма или введи своё:",
        reply_markup=get_main_keyboard()
    )
    await message.answer("Выбери время:", reply_markup=get_time_keyboard())
    await state.set_state(MedicineStates.waiting_times)

@router.message(MedicineStates.waiting_time_custom)
async def handle_time_custom_input(message: Message, state: FSMContext):
    """Ввод своего времени"""
    parsed = parse_times(message.text)
    
    if not parsed:
        await message.answer("❌ Формат времени: 09:00 или 09:00,21:00\n\nПопробуй ещё раз:")
        return
    
    await state.update_data(times=parsed)
    temp_state = ensure_temp_state(message.from_user.id)
    temp_state["times"] = parsed
    temp_state["step"] = "await_freq"
    
    await message.answer(
        f"✅ Время: {', '.join(parsed)}\n\n"
        "Выбери частоту приёма:",
        reply_markup=get_main_keyboard()
    )
    await message.answer("Частота:", reply_markup=get_frequency_keyboard())

@router.message(F.text == "📋 Мой список")
async def handle_show_list(message: Message):
    """Показать список препаратов"""
    medications = db.get_medications(message.from_user.id)
    
    if not medications:
        await message.answer(
            "❌ Пока нет препаратов!\n\n"
            "Нажми '➕ Добавить препарат'",
            reply_markup=get_main_keyboard()
        )
        return
    
    text_lines = ["📋 ТВОЙ СПИСОК ПРЕПАРАТОВ:\n"]
    
    for med_name, med_data in medications.items():
        times_str = ", ".join(med_data['times'])
        text_lines.append(f"💊 {med_name}")
        text_lines.append(f"  🕐 {times_str}")
        text_lines.append(f"  📅 {med_data['frequency']}")
        
        # Показываем AI-рекомендации если есть
        if med_data.get('best_time'):
            text_lines.append(f"  ⏰ Лучшее время: {med_data['best_time']}")
        if med_data.get('food_timing'):
            text_lines.append(f"  🍽️ {med_data['food_timing']}")
        
        text_lines.append("")
    
    await message.answer("\n".join(text_lines), reply_markup=get_main_keyboard())

@router.message(F.text == "📊 Отчёт")
async def handle_weekly_report(message: Message):
    """Отчёт за неделю"""
    report = db.get_week_report(message.from_user.id)
    await message.answer(report, reply_markup=get_main_keyboard())

@router.message(F.text == "🗑️ Удалить препарат")
async def handle_delete_start(message: Message, state: FSMContext):
    """Начало удаления препарата"""
    medications = db.get_medications(message.from_user.id)
    
    if not medications:
        await message.answer(
            "❌ Нет препаратов для удаления!\n\n"
            "Сначала добавь препараты.",
            reply_markup=get_main_keyboard()
        )
        return
    
    keyboard = get_medication_buttons(medications)
    await message.answer(
        "Какой препарат удалить?\n\n"
        "Выбери из списка ниже 👇",
        reply_markup=keyboard
    )
    await state.set_state(MedicineStates.waiting_delete_confirmation)

@router.message(MedicineStates.waiting_delete_confirmation, F.text.startswith("🗑️ "))
async def handle_delete_confirm(message: Message, state: FSMContext):
    """Подтверждение удаления"""
    med_name = message.text.replace("🗑️ ", "").strip()
    medications = db.get_medications(message.from_user.id)
    
    if med_name not in medications:
        await message.answer(
            "❌ Препарат не найден!\n\n"
            "Попробуй ещё раз или нажми ↩️ Отмена"
        )
        return
    
    success = db.delete_medication(message.from_user.id, med_name)
    
    if success:
        await message.answer(
            f"✅ Препарат удалён!\n\n"
            f"🗑️ {med_name}\n\n"
            "Больше не буду напоминать об этом препарате.",
            reply_markup=get_main_keyboard()
        )
        
        try:
            await bot.send_message(
                config.MANAGER_ID,
                f"🗑️ Удалён препарат: {med_name}\n"
                f"👤 Пользователь: {message.from_user.id}"
            )
        except Exception as e:
            logger.error(f"❌ Ошибка уведомления менеджера: {e}")
    else:
        await message.answer(
            "❌ Ошибка при удалении!\n\n"
            "Попробуй ещё раз.",
            reply_markup=get_main_keyboard()
        )
    
    await state.clear()

@router.message(MedicineStates.waiting_delete_confirmation, F.text == "↩️ Отмена")
async def handle_delete_cancel(message: Message, state: FSMContext):
    """Отмена удаления"""
    await message.answer(
        "Отменено. Ничего не удалено.",
        reply_markup=get_main_keyboard()
    )
    await state.clear()

@router.message(F.text == "❓ Помощь")
async def handle_help(message: Message):
    """Справка"""
    help_text = """📚 КАК ПОЛЬЗОВАТЬСЯ:

➕ Добавить препарат → Добавить новое лекарство
📋 Мой список → Информация о всех препаратах
🗑️ Удалить препарат → Удалить ненужное лекарство
📊 Отчёт → Статистика за последнюю неделю

💡 КОГДА ПРИХОДИТ НАПОМИНАНИЕ:
Бот пишет: "💊 Пора принять ГЛИЦИН!"
Нажми кнопку [✅ ПРИНЯЛ] когда примешь препарат

🤖 AI-ФУНКЦИИ:
/ask Вопрос — задать вопрос AI-консультанту
При добавлении препарата автоматически проверяется совместимость

⚡ БЫСТРЫЕ КОМАНДЫ:
/add Название 09:00,21:00 daily — быстро добавить
/delete Название — быстро удалить
"""
    await message.answer(help_text, reply_markup=get_main_keyboard())

@router.message(F.text.startswith("✅ ПРИНЯЛ"))
async def handle_taken_button(message: Message):
    """Обработка кнопки 'ПРИНЯЛ'"""
    text = message.text
    med_name = text.replace("✅ ПРИНЯЛ ", "").strip()
    current_time = datetime.now().strftime("%H:%M")
    
    success = db.mark_taken(message.from_user.id, med_name, current_time)
    
    if success:
        await message.answer(
            f"✅ Отмечено! {med_name} принят в {current_time}\n\n"
            "Молодец! 💚",
            reply_markup=get_main_keyboard()
        )
        
        try:
            await bot.send_message(
                config.MANAGER_ID,
                f"✅ Пользователь принял {med_name}\n"
                f"👤 ID: {message.from_user.id}\n"
                f"⏰ Время: {current_time}"
            )
        except Exception as e:
            logger.error(f"❌ Ошибка уведомления менеджера: {e}")
    else:
        await message.answer(
            f"❌ Препарат {med_name} не найден\n\n"
            "Проверь список препаратов",
            reply_markup=get_main_keyboard()
        )

# === CALLBACK HANDLERS (INLINE BUTTONS) ===

@router.callback_query(F.data.startswith("time_"))
async def callback_time_select(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора времени через inline-кнопки"""
    user_id = callback.from_user.id
    temp_state = ensure_temp_state(user_id)
    data = callback.data.replace("time_", "", 1)
    
    if data == "custom":
        temp_state["step"] = "await_time_custom"
        await callback.message.answer(
            "Напиши время: 09:00 или 09:00,21:00",
            reply_markup=get_main_keyboard()
        )
        await state.set_state(MedicineStates.waiting_time_custom)
        await callback.answer()
        return
    
    # Парсим выбранное время
    raw_time_data = data
    # Inline-кнопки могут отдавать '0900' вместо '09:00'
    if ':' not in raw_time_data:
        parts = [p.strip() for p in raw_time_data.split(',') if p.strip()]
        normalized_parts = []
        for p in parts:
            if len(p) == 4 and p.isdigit():
                normalized_parts.append(f"{p[:2]}:{p[2:]}")
            else:
                normalized_parts.append(p)
        raw_time_data = ','.join(normalized_parts)
    parsed = parse_times(raw_time_data)
    if not parsed:
        await callback.answer("Неверное время", show_alert=True)
        return
    
    temp_state["times"] = parsed
    temp_state["step"] = "await_freq"
    await state.update_data(times=parsed)
    
    await callback.message.answer(
        f"✅ Время: {', '.join(parsed)}\n\n"
        "Выбери частоту приёма:",
        reply_markup=get_frequency_keyboard()
    )
    await callback.answer()

@router.callback_query(F.data.startswith("freq_"))
async def callback_frequency_select(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора частоты через inline-кнопки"""
    user_id = callback.from_user.id
    temp_state = ensure_temp_state(user_id)
    
    if "med_name" not in temp_state or "times" not in temp_state:
        await callback.answer("Сначала название и время", show_alert=True)
        return
    
    key = callback.data
    
    if key == "freq_select":
        # Выбор конкретных дней недели
        temp_state["step"] = "select_days"
        temp_state.setdefault("selected_days", set())
        await callback.message.answer(
            "Выбери дни недели:",
            reply_markup=get_weekday_keyboard(user_id)
        )
        await callback.answer()
        return
    
    # Сохраняем препарат
    freq = frequency_label(key)
    med_name = temp_state["med_name"]
    times = temp_state["times"]
    
    # Проверяем AI-взаимодействия если включено
    ai_data = None
    if config.ENABLE_AI_FEATURES:
        user = db.get_user(user_id)
        if user and user.get("age"):
            existing_drugs = list(db.get_medications(user_id).keys())
            try:
                ai_data = await check_drug_interactions(med_name, existing_drugs, user['age'])
            except Exception as e:
                logger.error(f"AI check failed: {e}")
    
    db.reload()
    db.add_medication(user_id, med_name, times, freq, ai_data)
    
    response_text = f"✅ Добавлено!\n💊 {med_name}\n🕐 {', '.join(times)}\n📅 {freq}"
    
    # Добавляем AI-рекомендации если есть
    if ai_data:
        response_text += f"\n\n🤖 AI-РЕКОМЕНДАЦИИ:\n"
        response_text += f"⏰ Лучшее время: {ai_data.get('best_time')}\n"
        response_text += f"🍽️ Приём: {ai_data.get('food_timing')}"
        
        if ai_data.get('food_explanation'):
            response_text += f"\n   └─ {ai_data.get('food_explanation')}"
        
        if ai_data.get('side_effects'):
            response_text += f"\n\n⚠️ ВОЗМОЖНЫЕ ПОБОЧНЫЕ:\n"
            for effect in ai_data.get('side_effects', [])[:3]:
                response_text += f"  • {effect}\n"
        
        if ai_data.get('compatibility') == 'warning':
            response_text += f"\n⚠️ {ai_data.get('interactions')}"
        elif ai_data.get('compatibility') == 'danger':
            response_text += f"\n🚨 ВНИМАНИЕ: {ai_data.get('interactions')}"
    
    await callback.message.answer(response_text, reply_markup=get_main_keyboard())
    reset_temp_state(user_id)
    await state.clear()
    await callback.answer()

@router.callback_query(F.data.startswith("day_"))
async def callback_weekday_toggle(callback: CallbackQuery):
    """Переключение выбора дня недели"""
    user_id = callback.from_user.id
    temp_state = ensure_temp_state(user_id)
    
    if temp_state.get("step") != "select_days":
        await callback.answer()
        return
    
    code = callback.data.replace("day_", "", 1)
    selected: Set[str] = temp_state.setdefault("selected_days", set())
    
    if code in selected:
        selected.remove(code)
    else:
        selected.add(code)
    
    await callback.message.edit_reply_markup(reply_markup=get_weekday_keyboard(user_id))
    await callback.answer()

@router.callback_query(F.data == "save_days")
async def callback_save_weekdays(callback: CallbackQuery, state: FSMContext):
    """Сохранение выбранных дней недели"""
    user_id = callback.from_user.id
    temp_state = ensure_temp_state(user_id)
    
    if "med_name" not in temp_state or "times" not in temp_state:
        await callback.answer("Нет данных", show_alert=True)
        return
    
    selected: Set[str] = temp_state.get("selected_days", set())
    
    if not selected:
        await callback.answer("Выбери хотя бы один день", show_alert=True)
        return
    
    # Сохраняем препарат
    freq = frequency_label("freq_select", selected)
    med_name = temp_state["med_name"]
    times = temp_state["times"]
    
    # Проверяем AI-взаимодействия
    ai_data = None
    if config.ENABLE_AI_FEATURES:
        user = db.get_user(user_id)
        if user and user.get("age"):
            existing_drugs = list(db.get_medications(user_id).keys())
            try:
                ai_data = await check_drug_interactions(med_name, existing_drugs, user['age'])
            except Exception as e:
                logger.error(f"AI check failed: {e}")
    
    db.reload()
    db.add_medication(user_id, med_name, times, freq, ai_data)
    
    response_text = f"✅ Добавлено!\n💊 {med_name}\n🕐 {', '.join(times)}\n📅 {freq}"
    
    if ai_data:
        response_text += f"\n\n🤖 AI-РЕКОМЕНДАЦИИ:\n"
        response_text += f"⏰ {ai_data.get('best_time')}\n🍽️ {ai_data.get('food_timing')}"
    
    await callback.message.answer(response_text, reply_markup=get_main_keyboard())
    reset_temp_state(user_id)
    await state.clear()
    await callback.answer()

# === FALLBACK HANDLER ===

@router.message()
async def handle_any_message(message: Message):
    """Обработка неизвестных сообщений"""
    await message.answer(
        "🤔 Не понимаю...\n\n"
        "Используй кнопки снизу 👇",
        reply_markup=get_main_keyboard()
    )

# === ПЛАНИРОВЩИК ===

async def send_reminders():
    """Отправка напоминаний"""
    db.reload()
    
    if not db.data:
        logger.debug("📭 Нет пользователей")
        return
    
    now = datetime.now()
    current_time = now.strftime("%H:%M")
    current_day = WEEKDAY_MAP[now.weekday()]
    today = now.strftime("%Y-%m-%d")
    
    logger.info(f"⏰ Проверка напоминаний: {current_time} ({current_day})")
    
    for user_id_str, user_data in db.data.items():
        user_id = int(user_id_str)
        medications = user_data.get("medications", user_data)
        
        for med_name, med_data in medications.items():
            if med_name == "user_info":
                continue
            
            frequency = med_data['frequency']
            
            if not should_remind_today(frequency, current_day):
                continue
            
            for reminder_time in med_data['times']:
                if current_time != reminder_time:
                    continue
                
                # Проверяем, не принят ли уже
                already_taken = False
                if today in med_data.get('history', {}):
                    if reminder_time in med_data['history'][today]:
                        already_taken = med_data['history'][today][reminder_time]
                
                if already_taken:
                    continue
                
                text = f"""💊 НАПОМИНАНИЕ!

Пора принять: {med_name}

Нажми кнопку когда примешь 👇
"""
                
                try:
                    await bot.send_message(
                        user_id,
                        text,
                        reply_markup=get_taken_button(med_name)
                    )
                    logger.info(f"📢 Напоминание '{med_name}' → user {user_id}")
                except Exception as e:
                    logger.error(f"❌ Ошибка отправки напоминания: {e}")

async def send_weekly_report():
    """Еженедельный отчёт"""
    db.reload()
    
    for user_id_str in db.data.keys():
        user_id = int(user_id_str)
        report = db.get_week_report(user_id)
        
        try:
            await bot.send_message(user_id, report, reply_markup=get_main_keyboard())
            logger.info(f"📊 Отчёт → user {user_id}")
        except Exception as e:
            logger.error(f"❌ Ошибка отправки отчёта: {e}")

async def send_sos_alerts():
    """SOS-уведомления о пропущенных приёмах"""
    db.reload()
    
    for user_id_str in db.data.keys():
        user_id = int(user_id_str)
        missed = db.get_missed_reminders(user_id)
        
        if not missed:
            continue
        
        for med_name, reminder_time, reminder_dt in missed:
            hours_passed = int((datetime.now() - reminder_dt).total_seconds() / 3600)
            
            text = f"""🚨 SOS! ПРОПУЩЕН ПРИЁМ!

Препарат: {med_name}
Время было: {reminder_time}
Прошло: {hours_passed} ч.

Если уже принял(а) - нажми ✅
"""
            
            try:
                await bot.send_message(
                    user_id,
                    text,
                    reply_markup=get_taken_button(med_name)
                )
                
                await bot.send_message(
                    config.MANAGER_ID,
                    f"🚨 SOS!\n\n"
                    f"Пропущен: {med_name} ({reminder_time})\n"
                    f"👤 User: {user_id}\n"
                    f"⏰ {datetime.now().strftime('%d.%m %H:%M')}"
                )
                
                logger.warning(f"🚨 SOS {med_name} → user {user_id}")
            except Exception as e:
                logger.error(f"❌ Ошибка SOS: {e}")

async def send_morning_motivation():
    """Утренняя мотивация с AI"""
    if not config.ENABLE_MORNING_MOTIVATION or not ai_client:
        return
    
    db.reload()
    users = db.get_all_users()
    
    for user in users:
        if not user.get("onboarding_completed"):
            continue
        
        try:
            user_id = user["user_id"]
            name = user.get("name", "друг")
            streak = user.get("streak", 0)
            
            # Считаем прилежность за неделю
            medications = db.get_medications(user_id)
            total = 0
            taken = 0
            
            for med_data in medications.values():
                for i in range(7):
                    date_str = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                    if date_str in med_data.get('history', {}):
                        day_taken = sum(1 for v in med_data['history'][date_str].values() if v)
                        day_total = len(med_data['history'][date_str])
                        total += day_total
                        taken += day_taken
            
            adherence = int((taken / total) * 100) if total > 0 else 0
            
            # Генерируем текст
            text = await generate_morning_motivation_text(name, streak, adherence)
            
            await bot.send_message(user_id, f"☀️ {text}", reply_markup=get_main_keyboard())
            logger.info(f"🌅 Утренняя мотивация → user {user_id}")
            
            await asyncio.sleep(2)  # Rate limiting
        except Exception as e:
            logger.error(f"Morning motivation failed for user {user.get('user_id')}: {e}")

# === FASTAPI APP ===

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle events"""
    global bot, redis_client, dp
    
    # Переменная для polling task
    polling_task = None
    
    # Startup
    logger.info("🚀 Запуск Medicine Bot v3.0...")
    
    # Инициализация бота
    _validate_bot_token(config.BOT_TOKEN)
    bot = Bot(token=config.BOT_TOKEN)
    # Инициализация Redis
    try:
        redis_client = Redis(
            host=config.REDIS_HOST,
            port=config.REDIS_PORT,
            db=config.REDIS_DB,
            decode_responses=True
        )
        await redis_client.ping()
        logger.info("✅ Redis подключен")
        
        # Используем Redis Storage для FSM
        storage = RedisStorage(redis=redis_client)
        dp = Dispatcher(storage=storage)
    except Exception as e:
        logger.warning(f"⚠️ Redis недоступен, используется MemoryStorage: {e}")
        storage = MemoryStorage()
        dp = Dispatcher(storage=storage)
        redis_client = None
    
    # Регистрация роутера
    dp.include_router(router)
    
    # Настройка планировщика
    scheduler.add_job(send_reminders, 'interval', minutes=1, id='reminders')
    scheduler.add_job(
        send_weekly_report,
        'cron',
        day_of_week=config.REPORT_DAY,
        hour=config.REPORT_HOUR,
        minute=config.REPORT_MINUTE,
        id='weekly_report'
    )
    scheduler.add_job(send_sos_alerts, 'interval', hours=1, id='sos_alerts')
    
    if config.ENABLE_MORNING_MOTIVATION:
        scheduler.add_job(
            send_morning_motivation,
            'cron',
            hour=8,
            minute=0,
            id='morning_motivation'
        )
    
    scheduler.start()
    logger.info("⏰ Планировщик запущен")
    
    # Выбор режима работы: polling или webhook
    
    if config.USE_POLLING:
        # Отключаем webhook и запускаем polling
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Webhook отключен, запускаю polling (обход SSL)...")
        polling_task = asyncio.create_task(
            dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        )
    else:
        # Webhook режим
        webhook_url = f"https://bot-{config.BOT_TOKEN.split(':')[0]}.bothost.ru/webhook"
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            await bot.set_webhook(
                url=webhook_url,
                allowed_updates=dp.resolve_used_update_types(),
                drop_pending_updates=True
            )
            logger.info(f"✅ Webhook установлен: {webhook_url}")
            
            # Проверка статуса webhook
            webhook_info = await bot.get_webhook_info()
            if webhook_info.last_error_message:
                logger.warning(f"⚠️ Ошибка webhook: {webhook_info.last_error_message}")
            if webhook_info.pending_update_count > 0:
                logger.warning(f"⚠️ Накоплено обновлений: {webhook_info.pending_update_count}")
        except Exception as e:
            logger.error(f"❌ Ошибка установки webhook: {e}")
    
    yield
    
    # Shutdown
    logger.info("⛔ Остановка бота...")
    scheduler.shutdown()
    
    # Отменяем polling task, если он запущен
    if polling_task:
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass
        logger.info("✅ Polling остановлен")
    
    await bot.session.close()
    if redis_client:
        await redis_client.close()

# FastAPI app
app = FastAPI(lifespan=lifespan, title="Medicine Bot with AI")

@app.post("/webhook")
async def webhook(request: Request) -> Response:
    """Webhook endpoint для Telegram"""
    try:
        update_dict = await request.json()
        from aiogram.types import Update
        update = Update(**update_dict)
        await dp.feed_update(bot, update)
        return Response(status_code=200)
    except Exception as e:
        logger.error(f"❌ Ошибка обработки webhook: {e}")
        return Response(status_code=200)  # Всегда возвращаем 200, чтобы Telegram не повторял запрос

@app.get("/webhook-status")
async def webhook_status():
    """Проверка статуса webhook для диагностики"""
    try:
        webhook_info = await bot.get_webhook_info()
        return {
            "url": webhook_info.url,
            "has_custom_certificate": webhook_info.has_custom_certificate,
            "pending_update_count": webhook_info.pending_update_count,
            "last_error_date": webhook_info.last_error_date.isoformat() if webhook_info.last_error_date else None,
            "last_error_message": webhook_info.last_error_message,
            "max_connections": webhook_info.max_connections,
            "allowed_updates": webhook_info.allowed_updates,
            "status": "ok" if webhook_info.pending_update_count == 0 and not webhook_info.last_error_message else "warning"
        }
    except Exception as e:
        logger.error(f"❌ Ошибка получения статуса webhook: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/health")
async def health():
    """Health check для Bothost.ru"""
    return {
        "status": "ok",
        "bot": "Medicine Bot v3.0",
        "ai_enabled": config.ENABLE_AI_FEATURES,
        "features": {
            "onboarding": True,
            "ai_drug_check": config.ENABLE_AI_FEATURES,
            "morning_motivation": config.ENABLE_MORNING_MOTIVATION,
            "ai_consultant": config.ENABLE_AI_FEATURES
        }
    }

@app.get("/")
async def root():
    """Root endpoint"""
    return {"message": "Medicine Bot with AI is running!"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)