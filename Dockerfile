FROM python:3.12-slim
WORKDIR /app
COPY requirements-prod.txt .
RUN pip install --no-cache-dir -r requirements-prod.txt
COPY production ./production
COPY static ./static
CMD ["uvicorn","production.main:app","--host","0.0.0.0","--port","8080"]
