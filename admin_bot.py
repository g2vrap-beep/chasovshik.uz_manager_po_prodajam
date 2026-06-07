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

# Инициализация ИИ через OpenRouter на модель DeepSeek V4 Pro
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
        "Привет, Шеф! Универсальная система управления знаниями готова. Выбери режим работы:",
        reply_markup=get_main_keyboard()
    )

@dp.message(F.text == "📥 Режим загрузки")
async def enter_upload_mode(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.upload_mode)
    await message.answer(
        "Включен режим загрузки универсальных знаний. 📚\n\n"
        "Сюда можно загружать абсолютно всё:\n"
        "• Инструкции, книги, мануалы\n"
        "• Скрипты, регламенты, бизнес-логику\n\n"
        "Форматы: обычный текст или файлы **.txt** / **.pdf**.\n"
        "Выход: /start"
    )

@dp.message(F.text == "📊 Режим администрирования")
async def enter_admin_mode(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.admin_mode)
    db_url = os.getenv("DATABASE_URL")
    try:
        conn = await asyncpg.connect(db_url)
        nodes_count = await conn.fetchval("SELECT COUNT(*) FROM graph_nodes;")
        edges_count = await conn.fetchval("SELECT COUNT(*) FROM graph_edges;")
        await conn.close()
        
        await message.answer(
            f"📊 **Сводка из базы знаний:**\n\n"
            f"• Всего концептов/узлов в базе: {nodes_count}\n"
            f"• Логических связей между ними: {edges_count}\n\n"
            f"Выход: /start"
        )
    except Exception as e:
        await message.answer(f"Ошибка подключения к базе: {e}")

# Универсальный конвейер разбора и записи графа через DeepSeek
async def process_and_save_knowledge(text_content: str, message: types.Message):
    db_url = os.getenv("DATABASE_URL")
    
    # Промпт перенастроен на универсальное извлечение сущностей (не только продажи)
    system_prompt = (
        "Ты — универсальный ИИ-архитектор графов знаний. Твоя задача — проанализировать входящий текст "
        "(это может быть технический мануал, регламент, скрипт или книга) и разбить его на атомарные смысловые узлы "
        "и логические связи между ними.\n\n"
        "Типы узлов (node_type):\n"
        "- 'rule': жесткое правило, константа, инструкция, шаг алгоритма\n"
        "- 'objection': проблема, вводная ситуация, возражение, дефект, входящее условие\n"
        "- 'technique': метод решения, способ отработки, действие, техника выполнения\n\n"
        "Ты должен вернуть ответ СТРОГО в формате JSON. Никакого другого текста вокруг.\n"
        "Формат JSON:\n"
        "{\n"
        '  "nodes": [{"content": "краткое описание сути узла", "type": "rule" или "objection" или "technique"}],\n'
        '  "edges": [{"source_index": 0, "target_index": 1, "relation_type": "описание причинно-следственной связи"}]\n'
        "}"
    )
    
    try:
        # Подключаем киллер-фичу
        response = await openai_client.chat.completions.create(
            model="deepseek/deepseek-v4-pro",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Разбери этот текст на универсальный граф:\n\n{text_content}"}
            ],
            response_format={"type": "json_object"}
        )
        
        data = json.loads(response.choices[0].message.content)
        conn = await asyncpg.connect(db_url)
        inserted_node_ids = []
        
        for node in data.get("nodes", []):
            node_id = await conn.fetchval(
                "INSERT INTO graph_nodes (content, node_type) VALUES ($1, $2) RETURNING id;",
                node["content"], node["type"]
            )
            inserted_node_ids.append(node_id)
            
        edges_count = 0
        for edge in data.get("edges", []):
            src_idx = edge["source_index"]
            tgt_idx = edge["target_index"]
            
            if src_idx < len(inserted_node_ids) and tgt_idx < len(inserted_node_ids):
                await conn.execute(
                    "INSERT INTO graph_edges (source_id, target_id, relation_type) VALUES ($1, $2, $3);",
                    inserted_node_ids[src_idx], inserted_node_ids[tgt_idx], edge["relation_type"]
                )
                edges_count += 1
                
        await conn.close()
        
        await message.answer(
            f"🚀 **Киллер-фича отработала успешно!**\n"
            f"Данные внесены в архитектуру графа.\n\n"
            f"• Выделено смысловых блоков: {len(inserted_node_ids)}\n"
            f"• Найдено логических связей: {edges_count}"
        )
        
    except Exception as e:
        await message.answer(f"⚠️ Ошибка на стороне DeepSeek или Базы: {e}")

# Обработчик текста
@dp.message(AdminStates.upload_mode, F.text)
async def handle_text_upload(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    await process_and_save_knowledge(message.text, message)

# Обработчик файлов (PDF, TXT)
@dp.message(AdminStates.upload_mode, F.document)
async def handle_file_upload(message: types.Message):
    document = message.document
    file_name = document.file_name.lower()
    
    if not (file_name.endswith('.txt') or file_name.endswith('.pdf')):
        await message.answer("❌ Формат не поддерживается. Скинь файл .txt или .pdf")
        return
        
    await message.answer("⏳ Читаю документ, секунду...")
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
            text_parts = []
            for page in pdf_reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
            extracted_text = "\n".join(text_parts)
            
        if not extracted_text.strip():
            await message.answer("⚠️ Файл пустой или содержит только картинки.")
            return
            
        await message.answer(f"📖 Успешно извлечено {len(extracted_text)} символов. Передаю в DeepSeek V4 Pro...")
        await process_and_save_knowledge(extracted_text, message)
        
    except Exception as e:
        await message.answer(f"❌ Не удалось прочитать файл: {e}")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
