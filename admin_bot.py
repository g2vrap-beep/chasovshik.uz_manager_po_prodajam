import os
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from openai import AsyncOpenAI

# Инициализация ИИ через OpenRouter
openai_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY")
)

bot = Bot(token=os.getenv("TELEGRAM_ADMIN_TOKEN"))
dp = Dispatcher()

# Состояния бота (чтобы он помнил, в каком режиме мы находимся)
class AdminStates(StatesGroup):
    main_menu = State()
    upload_mode = State()
    admin_mode = State()

# Главное меню с кнопками
def get_main_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="📥 Режим загрузки")
    builder.button(text="📊 Режим администрирования")
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.main_menu)
    await message.answer(
        "Привет, Шеф! Я твой Бот-Админ.\nВыбери режим работы на панели ниже:",
        reply_markup=get_main_keyboard()
    )

# Вход в режим загрузки
@dp.message(F.text == "📥 Режим загрузки")
async def enter_upload_mode(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.upload_mode)
    await message.answer(
        "Включен режим загрузки знаний. 📚\n"
        "Отправляй мне текст, цитаты или инструкции по продажам. "
        "Я буду передавать их в Claude Sonnet для построения графа памяти.\n\n"
        "Чтобы выйти, нажми /start"
    )

# Вход в режим администрирования
@dp.message(F.text == "📊 Режим администрирования")
async def enter_admin_mode(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.admin_mode)
    # Пока базы нет, сделаем заглушку
    await message.answer(
        "Включен режим аналитики. 📈\n"
        "Задавай вопросы о том, что происходит в CRM или с заказами.\n\n"
        "Запрос к ИИ: Что сейчас происходит?\n"
        "Ответ (Заглушка): В системе зафиксировано 0 новых клиентов. База данных пуста.\n\n"
        "Чтобы выйти, нажми /start"
    )

# Логика обработки текста в режиме ЗАГРУЗКИ
@dp.message(AdminStates.upload_mode)
async def handle_upload(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    
    user_text = message.text
    
    # Отправляем запрос в OpenRouter (Claude 3.5 Sonnet)
    try:
        response = await openai_client.chat.completions.create(
            model="anthropic/claude-3.5-sonnet",
            messages=[
                {"role": "system", "content": "Ты системный аналитик. Твоя задача — принять текст по продажам/бизнесу и выделить главные тезисы, правила или возражения."},
                {"role": "user", "content": f"Проанализируй этот кусок книги/инструкции и выдели суть:\n\n{user_text}"}
            ]
        )
        ai_analysis = response.choices[0].message.content
        await message.answer(f"✅ **Текст принят и обработан Claude Sonnet!**\n\nВот что ИИ понял:\n{ai_analysis}")
        
        # ТОДО: Здесь будет код записи результатов анализа в Postgres (Граф)
        
    except Exception as e:
        await message.answer(f"Ошибка при обращении к OpenRouter: {e}")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
