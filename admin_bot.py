import os
import asyncio
import json
import io
from datetime import datetime, timezone
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from openai import AsyncOpenAI
import asyncpg
from pypdf import PdfReader

# Инициализация ИИ через OpenRouter
openai_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY")
)

bot = Bot(token=os.getenv("TELEGRAM_ADMIN_TOKEN"))
dp = Dispatcher()

# Состояния FSM
class AdminStates(StatesGroup):
    main_menu = State()
    upload_mode = State()
    processing = State()  # Блокировка процессов во время работы ИИ
    admin_mode = State()

# Главное меню (Reply-кнопки)
def get_main_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="📥 Режим загрузки")
    builder.button(text="📊 Режим admin-панели")
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)

# Меню режима загрузки (Reply-кнопки)
def get_upload_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="⬅️ Назад в меню")
    return builder.as_markup(resize_keyboard=True)

# Инлайн-клавиатура для главного экрана админки
def get_admin_main_inline():
    builder = InlineKeyboardBuilder()
    builder.button(text="📂 Управление файлами", callback_data="admin_manage_files")
    builder.button(text="🗑️ Очистить ВСЮ базу", callback_data="admin_confirm_clear_all")
    builder.adjust(1)
    return builder.as_markup()

# Хелпер для получения отсортированного списка уникальных файлов из БД
async def get_unique_files(conn) -> list:
    rows = await conn.fetch("""
        SELECT DISTINCT source_file FROM graph_nodes WHERE source_file IS NOT NULL
        UNION 
        SELECT DISTINCT source_file FROM graph_edges WHERE source_file IS NOT NULL;
    """)
    return sorted([r['source_file'] for r in rows])

# Функция создания вектора (эмбеддинга) смысла текста
async def get_embedding(text: str) -> list:
    try:
        response = await openai_client.embeddings.create(
            model="openai/text-embedding-3-small",
            input=text
        )
        return response.data[0].embedding
    except Exception as e:
        print(f"Ошибка генерации эмбеддинга: {e}")
        return []


# ================= ХЕНДЛЕР СТАРТА =================

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext, db_pool: asyncpg.Pool):
    await state.clear()
    
    try:
        async with db_pool.acquire() as conn:
            user_info = await conn.fetchrow("""
                SELECT a.role, a.company_id, c.company_name, c.subscription_expires_at 
                FROM admin_users a
                JOIN companies c ON a.company_id = c.id
                WHERE a.telegram_id = $1;
            """, message.from_user.id)
    except Exception as e:
        await message.answer(f"❌ Ошибка подключения к базе данных: {e}")
        return
        
    if not user_info:
        await message.answer("❌ **Доступ заблокирован.**\nВы не зарегистрированы в системе как администратор платформы.")
        return

    role = user_info['role']
    company_name = user_info['company_name']
    expires_at = user_info['subscription_expires_at']

    if role not in ["superadmin", "super_admin"]:
        now = datetime.now(timezone.utc) if expires_at and expires_at.tzinfo else datetime.now()
        if expires_at and expires_at < now:
            await message.answer(
                f"❌ **Доступ ограничен!**\n\n"
                f"У компании **{company_name}** закончился тестовый период или подписка.\n"
                f"Дата окончания доступа: {expires_at.strftime('%d.%m.%Y %H:%M')}.\n\n"
                f"Для продления периода обратитесь к владельцу платформы."
            )
            return

    await state.update_data(
        company_id=user_info['company_id'],
        role=role,
        company_name=company_name
    )
    
    await state.set_state(AdminStates.main_menu)
    await message.answer(
        f"👋 Добро пожаловать в панель управления!\n"
        f"🏢 Компания: **{company_name}**\n"
        f"🛡️ Ваша роль: `{role}`\n\n"
        f"Выбери режим работы:", 
        reply_markup=get_main_keyboard()
    )


# ================= ИСПРАВЛЕННАЯ КОМАНДА СУПЕР-АДМИНА =================

@dp.message(Command("grant"))
async def cmd_grant_days(message: types.Message, db_pool: asyncpg.Pool):
    try:
        async with db_pool.acquire() as conn:
            role = await conn.fetchval("SELECT role FROM admin_users WHERE telegram_id = $1;", message.from_user.id)
            
            if role not in ["superadmin", "super_admin"]:
                return

            args = message.text.split()[1:]
            if len(args) != 2:
                await message.answer(
                    "⚠️ **Неверный формат команды!**\n\n"
                    "Используй так:\n"
                    "• `/grant self 30` — выдать своей компании 30 дней\n"
                    "• `/grant 5 14` — выдать компании с ID 5 триал на 14 дней",
                    parse_mode="Markdown"
                )
                return
                
            target, days_str = args[0], args[1]
            
            if not days_str.isdigit():
                await message.answer("❌ Количество дней должно быть целым числом.")
                return
                
            days = int(days_str)
            
            if target.lower() == "self":
                company_id = await conn.fetchval("SELECT company_id FROM admin_users WHERE telegram_id = $1;", message.from_user.id)
                if not company_id:
                    await message.answer("❌ Ошибка: у вашей учетной записи не привязан ID компании.")
                    return
            else:
                if not target.isdigit():
                    await message.answer("❌ ID компании должно быть числом или ключевым словом `self`.")
                    return
                company_id = int(target)

            company_name = await conn.fetchval("SELECT company_name FROM companies WHERE id = $1;", company_id)
            if not company_name:
                await message.answer(f"❌ Компания с ID `{company_id}` не найдена в системе.")
                return
            
            # ИСПРАВЛЕНО: используем математическое умножение интервала ($1 * INTERVAL '1 day') вместо склеивания строк
            await conn.execute("""
                UPDATE companies 
                SET subscription_expires_at = CASE 
                    WHEN subscription_expires_at > CURRENT_TIMESTAMP THEN subscription_expires_at + ($1 * INTERVAL '1 day')
                    ELSE CURRENT_TIMESTAMP + ($1 * INTERVAL '1 day')
                END
                WHERE id = $2;
            """, days, company_id)
            
            new_expire_date = await conn.fetchval("SELECT subscription_expires_at FROM companies WHERE id = $1;", company_id)
            formatted_date = new_expire_date.strftime('%d.%m.%Y %H:%M')

        await message.answer(
            f"👑 **Власть над временем успешно применена!**\n\n"
            f"🏢 Компания: **{company_name}** (ID: {company_id})\n"
            f"➕ Добавлено дней: `{days}`\n"
            f"📅 Новая дата окончания доступа: **{formatted_date}**"
        )
        
    except Exception as e:
        await message.answer(f"❌ Ошибка при изменении времени подписки: {e}")


# ================= РЕЖИМЫ РАБОТЫ И КОНВЕЙЕРЫ (БЕЗ ИЗМЕНЕНИЙ) =================

# Вход в режим загрузки
@dp.message(F.text == "📥 Режим загрузки")
async def enter_upload_mode(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.upload_mode)
    await message.answer("Режим загрузки активирован. Отправь текст или .pdf/.txt файл.", reply_markup=get_upload_keyboard())

# Возврат из режима загрузки в главное меню
@dp.message(AdminStates.upload_mode, F.text == "⬅️ Назад в меню")
async def back_to_main(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.main_menu)
    await message.answer("Вы вернулись в главное меню.", reply_markup=get_main_keyboard())

# Вход в админ-панель (отображение статистики и инлайн-меню)
@dp.message(F.text == "📊 Режим admin-панели")
async def enter_admin_mode(message: types.Message, state: FSMContext, db_pool: asyncpg.Pool):
    await state.set_state(AdminStates.admin_mode)
    try:
        async with db_pool.acquire() as conn:
            nodes_count = await conn.fetchval("SELECT COUNT(*) FROM graph_nodes;")
            edges_count = await conn.fetchval("SELECT COUNT(*) FROM graph_edges;")
        await message.answer(
            f"📊 **База знаний:**\n• Узлов графа: {nodes_count}\n• Логических связей: {edges_count}\n\n"
            f"Выберите действие ниже:", 
            reply_markup=get_admin_main_inline()
        )
    except Exception as e:
        await message.answer(f"Ошибка БД: {e}")

# ХЕНДЛЕР-ЗАГЛУШКА: жесткий щит от спама во время работы ИИ
@dp.message(AdminStates.processing)
async def handle_processing_lock(message: types.Message):
    await message.answer(
        "⚠️ **Пожалуйста, подождите!**\n"
        "Прямо сейчас я анализирую прошлые данные и строю граф знаний. "
        "Как только я закончу, я сразу вам сообщу!"
    )

# Конвейер разбора и векторной записи графа (с поддержкой source_file)
async def process_and_save_knowledge(text_content: str, message: types.Message, db_pool: asyncpg.Pool, source_file: str):
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
                
                embedding = await get_embedding(content)
                existing_node_id = None
                
                if embedding:
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
                    inserted_node_ids.append(existing_node_id)
                else:
                    node_id = await conn.fetchval(
                        """
                        INSERT INTO graph_nodes (content, node_type, embedding, source_file) 
                        VALUES ($1, $2, $3::vector, $4) 
                        RETURNING id;
                        """,
                        content, node_type, str(embedding), source_file
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
                        INSERT INTO graph_edges (source_id, target_id, relation_type, source_file) 
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT DO NOTHING;
                        """,
                        inserted_node_ids[src_idx], inserted_node_ids[tgt_idx], edge["relation_type"], source_file
                    )
                    edges_count += 1
                    
        await message.answer(
            f"🚀 **Интеграция блока завершена!**\n"
            f"• Файл: `{source_file}`\n"
            f"• Обработано блоков: {len(inserted_node_ids)}\n"
            f"• Создано связей: {edges_count}"
        )

# Хендлер загрузки чистого ТЕКСТА
@dp.message(AdminStates.upload_mode, F.text)
async def handle_text_upload(message: types.Message, state: FSMContext, db_pool: asyncpg.Pool):
    await state.set_state(AdminStates.processing)
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    
    source_name = "Ручной ввод текста"
    try:
        await process_and_save_knowledge(message.text, message, db_pool, source_name)
        await message.answer("🏁 **Вся информация успешно загружена и проанализирована!**")
    except Exception as e:
        await message.answer(f"⚠️ Ошибка обработки куска данных: {e}")
    finally:
        await state.set_state(AdminStates.upload_mode)
        await message.answer("теперь можете продолжать загружать в меня информацию.", reply_markup=get_upload_keyboard())

# Хендлер загрузки ФАЙЛОВ (.txt и .pdf)
@dp.message(AdminStates.upload_mode, F.document)
async def handle_file_upload(message: types.Message, state: FSMContext, db_pool: asyncpg.Pool):
    document = message.document
    file_name = document.file_name
    file_name_lower = file_name.lower()
    
    if not (file_name_lower.endswith('.txt') or file_name_lower.endswith('.pdf')):
        await message.answer("❌ Нужен файл в формате .txt или .pdf")
        return
        
    await state.set_state(AdminStates.processing)
    await message.answer("⏳ Начинаю чтение документа... Система заблокирована до конца обработки.", reply_markup=types.ReplyKeyboardRemove())
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    
    file_in_memory = io.BytesIO()
    await bot.download(document, destination=file_in_memory)
    file_in_memory.seek(0)
    
    try:
        if file_name_lower.endswith('.txt'):
            extracted_text = file_in_memory.read().decode('utf-8', errors='ignore')
            if not extracted_text.strip():
                await message.answer("⚠️ Файл пустой.")
                return
            await message.answer("📖 Текст считан. Отправляю в DeepSeek...")
            await process_and_save_knowledge(extracted_text, message, db_pool, file_name)
            
        elif file_name_lower.endswith('.pdf'):
            pdf_reader = PdfReader(file_in_memory)
            total_pages = len(pdf_reader.pages)
            await message.answer(f"📄 Обнаружено страниц в PDF: {total_pages}\nЗапускаю пошаговую обработку...")
            
            pages_per_chunk = 3  
            current_chunk_text = []
            chunk_counter = 1
            
            for page_num, page in enumerate(pdf_reader.pages, start=1):
                page_text = page.extract_text() or ""
                current_chunk_text.append(page_text)
                
                if page_num % pages_per_chunk == 0 or page_num == total_pages:
                    full_chunk_text = "\n".join(current_chunk_text).strip()
                    
                    if full_chunk_text:
                        await message.answer(f"🔄 Обрабатываю часть {chunk_counter} (страницы {page_num - len(current_chunk_text) + 1} - {page_num})...")
                        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
                        
                        await process_and_save_knowledge(full_chunk_text, message, db_pool, file_name)
                        chunk_counter += 1
                        await asyncio.sleep(1)  
                        
                    current_chunk_text = []
                    
        await message.answer("🏁 **Вся книга знаний успешно загружена и проанализирована!**")
            
    except Exception as e:
        await message.answer(f"❌ Критическая ошибка при чтении файла: {e}")
    finally:
        await state.set_state(AdminStates.upload_mode)
        await message.answer("теперь можете продолжать загружать в меня информацию.", reply_markup=get_upload_keyboard())


# ================= ИНЛАЙН ХЕНДЛЕРЫ АДМИН-ПАНЕЛИ =================

# 1. Вывод списка файлов для удаления
@dp.callback_query(AdminStates.admin_mode, F.data == "admin_manage_files")
async def inline_manage_files(callback: types.CallbackQuery, db_pool: asyncpg.Pool):
    await callback.answer()
    async with db_pool.acquire() as conn:
        files = await get_unique_files(conn)
        
    if not files:
        await callback.message.edit_text("📂 В базе знаний пока нет загруженных файлов.", reply_markup=get_admin_main_inline())
        return
        
    text = "📂 **Список загруженных файлов в БД:**\nНажмите на кнопку с номером, чтобы удалить конкретный файл:\n\n"
    builder = InlineKeyboardBuilder()
    
    for idx, filename in enumerate(files, start=1):
        text += f"{idx}. `{filename}`\n"
        builder.button(text=f"🗑️ Удалить {idx}", callback_data=f"del_file_{idx-1}")
        
    builder.button(text="⬅️ Назад в админку", callback_data="admin_to_main")
    builder.adjust(2)
    
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())

# 2. Обработка удаления КОНКРЕТНОГО файла по его индексу
@dp.callback_query(AdminStates.admin_mode, F.data.startswith("del_file_"))
async def inline_delete_specific_file(callback: types.CallbackQuery, db_pool: asyncpg.Pool):
    file_idx = int(callback.data.split("_")[2])
    
    async with db_pool.acquire() as conn:
        files = await get_unique_files(conn)
        
        if file_idx >= len(files):
            await callback.answer("❌ Файл уже не существует или список обновился.")
            return
            
        target_file = files[file_idx]
        await callback.answer(f"Удаляю {target_file}...")
        
        async with conn.transaction():
            await conn.execute("DELETE FROM graph_edges WHERE source_file = $1;", target_file)
            await conn.execute("""
                DELETE FROM graph_nodes 
                WHERE source_file = $1 
                  AND id NOT IN (SELECT source_id FROM graph_edges UNION SELECT target_id FROM graph_edges);
            """, target_file)
            
    await inline_manage_files(callback, db_pool)

# 3. Возврат на главный экран админки
@dp.callback_query(AdminStates.admin_mode, F.data == "admin_to_main")
async def inline_back_to_admin_main(callback: types.CallbackQuery, db_pool: asyncpg.Pool):
    await callback.answer()
    async with db_pool.acquire() as conn:
        nodes_count = await conn.fetchval("SELECT COUNT(*) FROM graph_nodes;")
        edges_count = await conn.fetchval("SELECT COUNT(*) FROM graph_edges;")
    await callback.message.edit_text(
        f"📊 **База знаний:**\n• Узлов графа: {nodes_count}\n• Логических связей: {edges_count}\n\n"
        f"Выберите действие ниже:", 
        reply_markup=get_admin_main_inline()
    )

# 4. Запрос на полное очищение базы (защита от случайного клика)
@dp.callback_query(AdminStates.admin_mode, F.data == "admin_confirm_clear_all")
async def inline_confirm_clear(callback: types.CallbackQuery):
    await callback.answer()
    builder = InlineKeyboardBuilder()
    builder.button(text="💥 ДА, СТЕРЕТЬ ВСЁ", callback_data="admin_action_clear_all")
    builder.button(text="❌ ОТМЕНА", callback_data="admin_to_main")
    builder.adjust(1)
    await callback.message.edit_text("⚠️ **ВНИМАНИЕ!** Вы уверены, что хотите полностью очистить базу знаний? Это действие необратимо.", reply_markup=builder.as_markup())

# 5. Выполнение полной очистки базы
@dp.callback_query(AdminStates.admin_mode, F.data == "admin_action_clear_all")
async def inline_execute_clear_all(callback: types.CallbackQuery, db_pool: asyncpg.Pool):
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("TRUNCATE graph_edges, graph_nodes RESTART IDENTITY CASCADE;")
        await callback.answer("База полностью очищена!", show_alert=True)
    except Exception as e:
        await callback.answer(f"Ошибка: {e}", show_alert=True)
        
    await inline_back_to_admin_main(callback, db_pool)


# Главная функция запуска
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
