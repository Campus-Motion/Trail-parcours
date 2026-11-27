FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then \
      PIP_CERT=/run/secrets/proxy_ca pip install --no-cache-dir -r requirements.txt; \
    else pip install --no-cache-dir -r requirements.txt; fi
COPY --chmod=644 api.py heatmap_v2.py ./
USER 10001:10001
EXPOSE 3041
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "3041"]
