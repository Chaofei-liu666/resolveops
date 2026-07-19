FROM python:3.12-slim AS runtime
WORKDIR /app
COPY requirements-prod.txt .
RUN pip install --no-cache-dir -r requirements-prod.txt
COPY production ./production
COPY static ./static
CMD ["uvicorn","production.main:app","--host","0.0.0.0","--port","8080"]

FROM runtime AS test
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY app.py ./app.py
COPY tests ./tests
CMD ["python","-m","pytest","-q"]
