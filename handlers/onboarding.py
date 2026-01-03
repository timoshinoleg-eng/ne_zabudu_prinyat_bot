"""
Онбординг новых пользователей
"""
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from database.db import db

router = Router()

class OnboardingStates(StatesGroup):
    waiting_name = State()
    waiting_age = State()

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """Приветствие"""
    user = db.get_user(message.from_user.id)
    
    if user and user.get("onboarding_completed"):
        # Уже зарегистрирован
        await message.answer(
            f"С возвращением, {user['name']}! 👋\n\n"
            "Что будем делать?",
            reply_markup=get_main_keyboard()
        )
        return
    
    # Новый пользователь
    await message.answer(
        "👋 Привет! Я твой персональный помощник по лекарствам!\n\n"
        "Помогу не забывать принимать препараты и позабочусь о твоём здоровье 💊\n\n"
        "Давай познакомимся! Как к тебе обращаться?"
    )
    await state.set_state(OnboardingStates.waiting_name)

@router.message(OnboardingStates.waiting_name)
async def handle_name(message: Message, state: FSMContext):
    """Получение имени"""
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
async def handle_age(message: Message, state: FSMContext):
    """Получение возраста"""
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
