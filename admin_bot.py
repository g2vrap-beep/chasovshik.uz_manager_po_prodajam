import os
import asyncio
import json
import io
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from openai import AsyncOpenAI
import asyncpg
from pypdf import PdfReader

# Инициализация ИИ
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
    builder.button(text="📊 Режим admin-панели")
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)

# Функция создания вектора (эмбеддинга) смысла текста
async def get_embedding(text: str) -> list:
    try:
        response = await openai_client.embeddings.create(
            model="openai/text-embedding-3-small",  # Отличная, быстрая и дешевая модель
            input=text
        )
        return response.data[0].embedding
    except Exception as e:
        print(f"Ошибка генерации эмбеддинга: {e}")
        return []

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.main_menu)
    await message.answer("Система управления знаниями запущена. Выбери режим:", reply_markup=get_main_keyboard())

@dp.message(F.text == "📥 Режим загрузки")
async def enter_upload_mode(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.upload_mode)
    await message.answer("Режим загрузки активирован. Отправь текст или .pdf/.txt файл. Выход: /start")

@dp.message(F.text == "📊 Режим admin-панели")
async def enter_admin_mode(message: types.Message, state: FSMContext, db_pool: asyncpg.Pool):
    await state.set_state(AdminStates.admin_mode)
    try:
        async with db_pool.acquire() as conn:
            nodes_count = await conn.fetchval("SELECT COUNT(*) FROM graph_nodes;")
            edges_count = await conn.fetchval("SELECT COUNT(*) FROM graph_edges;")
        await message.answer(f"📊 **База знаний:**\n• Узлов: {nodes_count}\n• Связей: {edges_count}\n\nВыход: /start")
    except Exception as e:
        await message.answer(f"Ошибка БД: {e}")

# Конвейер разбора и векторной записи графа
async def process_and_save_knowledge(text_content: str, message: types.Message, db_pool: asyncpg.Pool):
    system_prompt = (
        "Ты — универсальный ИИ-архитектор графов знаний. Твоя задача — проанализировать входящий текст "
        "и разбить его на атомарные смысловые узлы и логические связи между ними.\n\n"
        "Типы узлов (node_type):\n"
        "- 'rule': жесткое правило, инструкция, шаг алгоритма\n"
        "- 'objection': проблема, вводная ситуация, возражение, входящее условие\n"
        "- 'technique': метод решения, действие, техника выполнения\n\n"
        "Ты должен вернуть ответ СТРОГО в формате JSON.\n"
        "Формат JSON:\n"
        "{\n"
        '  "nodes": [{"content": "краткое описание сути узла", "type": "rule"||"objection"||"technique"}],\n'
        '  "edges": [{"source_index": 0, "target_index": 1, "relation_type": "описание связи"}]\n'
        "}"
    )
    
    try:
        response = await openai_client.chat.completions.create(
            model="deepseek/deepseek-v4-pro",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Разбери текст на граф:\n\n{text_content}"}
            ],
            response_format={"type": "json_object"}
        )
        
        data = json.loads(response.choices[0].message.content)
        
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                inserted_node_ids = []
                
                # Обработка Узлов
                for node in data.get("nodes", []):
                    content = node["content"]
                    node_type = node["type"]
                    
                    # Получаем вектор смысла для текущего узла
                    embedding = await get_embedding(content)
                    
                    existing_node_id = None
                    if embedding:
                        # Ищем в базе узел, схожесть с которым выше 85% (0.85)
                        # В pgvector `<=>` — это косинусное расстояние. Схожесть = 1 - расстояние.
                        existing_node_id = await conn.fetchval(
                            """
                            SELECT id FROM graph_nodes 
                            WHERE 1 - (embedding <=> $1::vector) > 0.70 
                            ORDER BY embedding <=> $1::vector 
                            LIMIT 1;
                            """,
                            str(embedding)
                        )
                    
                    if existing_node_id:
                        # Если похожий по смыслу узел найден — используем его ID (не создаем дубль!)
                        inserted_node_ids.append(existing_node_id)
                    else:
                        # Если это принципиально новая мысль — записываем её и её вектор
                        node_id = await conn.fetchval(
                            """
                            INSERT INTO graph_nodes (content, node_type, embedding) 
                            VALUES ($1, $2, $3) 
                            RETURNING id;
                            """,
                            content, node_type, str(embedding)
                        )
                        inserted_node_ids.append(node_id)
                        
                edges_count = 0
                # Обработка Связей
                for edge in data.get("edges", []):
                    src_idx = edge["source_index"]
                    tgt_idx = edge["target_index"]
                    
                    if src_idx < len(inserted_node_ids) and tgt_idx < len(inserted_node_ids):
                        await conn.execute(
                            """
                            INSERT INTO graph_edges (source_id, target_id, relation_type) 
                            VALUES ($1, $2, $3)
                            ON CONFLICT DO NOTHING;
                            """,
                            inserted_node_ids[src_idx], inserted_node_ids[tgt_idx], edge["relation_type"]
                        )
                        edges_count += 1
                        
        await message.answer(
            f"🚀 **Векторная интеграция завершена!**\n\n"
            f"• Обработано блоков: {len(inserted_node_ids)}\n"
            f"• Проверено/создано связей: {edges_count}"
        )
        
    except Exception as e:
        await message.answer(f"⚠️ Ошибка: {e}")

# Хендлеры
@dp.message(AdminStates.upload_mode, F.text)
async def handle_text_upload(message: types.Message, db_pool: asyncpg.Pool):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    await process_and_save_knowledge(message.text, message, db_pool)

@dp.message(AdminStates.upload_mode, F.document)
async def handle_file_upload(message: types.Message, db_pool: asyncpg.Pool):
    document = message.document
    file_name = document.file_name.lower()
    
    if not (file_name.endswith('.txt') or file_name.endswith('.pdf')):
        await message.answer("❌ Нужен .txt или .pdf")
        return
        
    await message.answer("⏳ Анализирую файл...")
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    
    file_in_memory = io.BytesIO()
    await bot.download(document, destination=file_in_memory)
    file_in_memory.seek(0)
    
    extracted_text = ""
    try:
        if file_name.endswith('.txt'):
            extracted_text = file_in_memory.read().decode('utf-8', errors='ignore')
        elif file_name.endswith('.pdf'):
            pdf_reader = PdfReader(file_in_memory)
            extracted_text = "\n".join([page.extract_text() or "" for page in pdf_reader.pages])
            
        if not extracted_text.strip():
            await message.answer("⚠️ Файл пустой.")
            return
            
        await process_and_save_knowledge(extracted_text, message, db_pool)
        
    except Exception as e:
        await message.answer(f"❌ Ошибка чтения: {e}")

async def main():
    db_url = os.getenv("DATABASE_URL")
    pool = await asyncpg.create_pool(dsn=db_url, min_size=1, max_size=10)
    dp["db_pool"] = pool
    try:
        await dp.start_polling(bot)
    finally:
        await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
