# Rain Alert Voice Bot — container image
FROM python:3.11-slim

WORKDIR /app

# Install dependencies first so this layer is cached unless requirements change
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY main.py .

# The bot needs these at runtime — pass them with `docker run --env-file .env`
# or via your platform's secrets/env config (Railway, Render, etc.). This
# image intentionally does not bake in a .env file.
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
