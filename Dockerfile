FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

RUN python -c "import aiohttp; import numpy; import pandas; print('deps OK')"

CMD ["python", "-m", "bot.main"]
