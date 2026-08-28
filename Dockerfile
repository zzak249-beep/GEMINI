# Alternativa a Nixpacks para desplegar en Railway (u otro host con Docker)
# con control explícito del entorno.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# El bot no sirve tráfico web: no se expone ningún puerto a propósito.
CMD ["python", "bot.py"]
