"""
AI-консультации по лекарствам
"""
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message

from ai.openrouter import openrouter_client
from utils.rate_limiter import check_ai_limit

router = Router()

@router.message(Command("ask"))
async def cmd_ask(message: Message):
    """Команда /ask для вопросов"""
    
    # Проверяем лимит
    allowed, remaining = await check_ai_limit(message.from_user.id, "ask")
    if not allowed:
        await message.answer(
            "❌ Исчерпан лимит AI-запросов на сегодня (5/день)\n\n"
            "Попробуй завтра или заработай дополнительные запросы выполнением достижений!"
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
    
    prompt = f"""Ты медицинский консультант. Ответь на вопрос пользователя.

ВОПРОС: {question}

КОНТЕКСТ ПОЛЬЗОВАТЕЛЯ:
- Возраст: {user.get('age')} лет
- Принимаемые препараты: {', '.join(medications.keys()) if medications else 'нет'}

Дай краткий, понятный ответ (3-4 предложения).
Используй эмодзи для наглядности.
В конце добавь: "⚠️ Это общая информация. При сомнениях — консультируйся с врачом."
"""
    
    response = await openrouter_client.complete(prompt)
    
    await message.answer(
        f"🤖 AI-КОНСУЛЬТАНТ:\n\n{response}\n\n"
        f"Осталось запросов сегодня: {remaining}"
    )
