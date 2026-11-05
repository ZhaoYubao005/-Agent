FROM python:3.12-slim-bookworm

WORKDIR /app
ENV PYTHONPATH=/app/agent-service \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.lock ./
RUN python -m pip install --no-cache-dir -r requirements.lock \
    && useradd --system --create-home --uid 10001 --user-group ecomops \
    && install -d -o ecomops -g ecomops /data
COPY agent-service/ agent-service/
COPY mcp-server/ mcp-server/

USER ecomops
CMD ["python", "-m", "uvicorn", "ecomops.app:app", "--host", "0.0.0.0", "--port", "8100"]
