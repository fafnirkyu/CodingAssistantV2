FROM python:3.10-slim

# Install C++ compiler for llama-cpp
RUN apt-get update && apt-get install -y build-essential && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Railway uses the PORT env var
ENV PORT=8080
EXPOSE 8080

#creates user appuser and sets ownership of /app to that user
RUN useradd -m appuser
RUN chown -R appuser:appuser /app

USER appuser

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "backend.app:app"]