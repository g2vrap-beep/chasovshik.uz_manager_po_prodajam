import os
import asyncio
import json
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from openai import AsyncOpenAI
import asyncpg

# Инициализация ИИ через OpenRouter
openai_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY")
)

bot = Bot(token=os.getenv("TELEGRAM_ADMIN_TOKEN"))
dp = Dispatcher()

class AdminStates(StatesGroup):
    main_menu = State()
    upload_mode = State()
    admin_mode = State()

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
        "Привет, Шеф! Система готова. Выбери режим работы:",
        reply_markup=get_main_keyboard()
    )

@dp.message(F.text == "📥 Режим загрузки")
async def enter_upload_mode(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.upload_mode)
    await message.answer(
        "Включен режим загрузки знаний. 📚\n"
        "Отправь мне правило, цитату или скрипт продаж. "
        "Я проанализирую текст через Claude Sonnet и сохраню граф связей в базу.\n\n"
        "Выход: /start"
    )

@dp.message(F.text == "📊 Режим администрирования")
async def enter_admin_mode(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.admin_mode)
    
    # Считаем реальное количество знаний в базе через SQL
    db_url = os.getenv("DATABASE_URL")
    try:
        conn = await asyncpg.connect(db_url)
        nodes_count = await conn.fetchval("SELECT COUNT(*) FROM graph_nodes;")
        edges_count = await conn.fetchval("SELECT COUNT(*) FROM graph_edges;")
        await conn.close()
        
        await message.answer(
            f"📊 **Сводка из базы знаний:**\n\n"
            f"• Загружено правил/узлов: {nodes_count}\n"
            f"• Создано связей между ними: {edges_count}\n\n"
            f"Сводка по клиентам из CRM будет доступна после подключения клиентского модуля.\n\n"
            f"Выход: /start"
        )
    except Exception as e:
        await message.answer(f"Ошибка подключения к базе: {e}")

# ЛОГИКА АВТОМАТИЧЕСКОГО РАЗБОРА И ЗАПИСИ ГРАФА
@dp.message(AdminStates.upload_mode)
async def handle_upload(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    user_text = message.text
    db_url = os.getenv("DATABASE_URL")
    
    # Просим Sonnet вернуть СТРОГИЙ JSON формат
    system_prompt = (
        "Ты — ИИ-архитектор графов знаний. Твоя задача — выделить из текста правила, возражения и техники, "
        "а также связи между ними. Ты должен вернуть ответ СТРОГО в формате JSON. Никакого другого текста вокруг.\n"
        "Формат JSON:\n"
        "{\n"
        '  "nodes": [{"content": "текст узла", "type": "rule" или "objection" или "technique"}],\n'
        '  "edges": [{"source_index": 0, "target_index": 1, "relation_type": "описание связи"}]\n'
        "}"
    )
    
    try:
        response = await openai_client.chat.completions.create(
            model="anthropic/claude-3.5-sonnet",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Разбери этот текст на граф:\n\n{user_text}"}
            ],
            response_format={"type": "json_object"} # Гарантирует JSON на выходе
        )
        
        # Парсим то, что прислал Sonnet
        data = json.loads(response.choices[0].message.content)
        
        conn = await asyncpg.connect(db_url)
        inserted_node_ids = []
        
        # 1. Записываем узлы (Nodes)
        for node in data.get("nodes", []):
            node_id = await conn.fetchval(
                "INSERT INTO graph_nodes (content, node_type) VALUES ($1, $2) RETURNING id;",
                node["content"], node["type"]
            )
            inserted_node_ids.append(node_id)
            
        # 2. Записываем связи (Edges), используя сохраненные ID
        edges_count = 0
        for edge in data.get("edges", []):
            src_idx = edge["source_index"]
            tgt_idx = edge["target_index"]
            
            # Проверяем, что индексы корректны
            if src_idx < len(inserted_node_ids) and tgt_idx < len(inserted_node_ids):
                await conn.execute(
                    "INSERT INTO graph_edges (source_id, target_id, relation_type) VALUES ($1, $2, $3);",
                    inserted_node_ids[src_idx], inserted_node_ids[tgt_idx], edge["relation_type"]
                )
                edges_count += 1
                
        await conn.close()
        
        await message.answer(
            f"✅ **Успешно загружено в базу!**\n\n"
            f" Сохранено новых узлов: {len(inserted_node_ids)}\n"
            f" Создано связей между ними: {edges_count}\n\n"
            f"Ты можешь проверить их в Railway во вкладке Data."
        )
        
    except Exception as e:
        await message.answer(f"⚠️ Произошла ошибка при обработке: {e}")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
