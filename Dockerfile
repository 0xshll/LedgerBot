FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/app/data/accounts.sqlite3

WORKDIR /app

RUN mkdir -p /app/data

EXPOSE 8080

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py script.py ./

CMD ["python", "-u", "bot.py"]
