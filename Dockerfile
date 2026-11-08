FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api.py heatmap_v2.py ./
USER 10001:10001
EXPOSE 3041
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "3041"]
