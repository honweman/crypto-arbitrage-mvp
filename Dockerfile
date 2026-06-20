FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY config.example.json config.acs.example.json ./

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -e .

EXPOSE 8080

CMD ["python", "-m", "arbitrage_bot.web", "--config", "config.acs.json", "--strategy", "all", "--host", "0.0.0.0", "--port", "8080"]
