# 1. Используем сверхуверенную и стабильную версию Python
FROM python:3.12-slim

# 2. Настройки для моментального вывода логов в панель Railway (без задержек)
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 3. Устанавливаем системные пакеты, без которых ломаются базы данных (Postgres/asyncpg)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 4. Обновляем установщик и качаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 5. Копируем файлы твоего бота в облако
COPY . .

# 6. Команда запуска (проверь, что твой главный файл называется именно admin_bot.py)
CMD ["python", "admin_bot.py"]
