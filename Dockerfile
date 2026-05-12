FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_SYSTEM_PYTHON=1

WORKDIR /app

# Install system dependencies if any needed (e.g. for azure-cli)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast package management
RUN pip install uv

# Copy project configuration
COPY pyproject.toml .

# Install dependencies
RUN uv pip install -r pyproject.toml --system

# Copy application code
COPY resource_advisor /app/resource_advisor

# Expose port
EXPOSE 8000

# Run the API server
CMD ["uvicorn", "resource_advisor.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
