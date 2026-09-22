FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY backend/pyproject.toml ./
COPY backend/app ./app
COPY backend/migrations ./migrations
COPY backend/alembic.ini ./
COPY src/map_integration.py ./src/map_integration.py

RUN pip install --no-cache-dir .

# Keep frontend assets after dependency installation so UI-only changes rebuild quickly.
COPY data/智拓商机作战助手-开发版.html ./frontend/index.html
COPY data/china-mobile-logo.svg ./frontend/china-mobile-logo.svg

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
