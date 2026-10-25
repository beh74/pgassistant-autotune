FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml ./
COPY requirements.lock ./
COPY app ./app
COPY sql ./sql
RUN pip install --no-cache-dir -c requirements.lock . && useradd --uid 10001 --create-home autotune
USER autotune
EXPOSE 8080
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
