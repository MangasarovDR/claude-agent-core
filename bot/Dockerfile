FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -q -r requirements.txt

# Устанавливаем системные зависимости Chromium вручную (--with-deps падает на
# ttf-unifont/ttf-ubuntu-font-family, которых нет в Debian 12)
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libgbm1 libxkbcommon0 libatspi2.0-0 libwayland-client0 \
        libasound2 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
RUN playwright install chromium

COPY . .

CMD ["python", "bot.py"]
