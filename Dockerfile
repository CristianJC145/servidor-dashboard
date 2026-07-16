FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
COPY static ./static
ENV DATA_DIR=/data
EXPOSE 8000
# --proxy-headers + forwarded-allow-ips: detrás del proxy de EasyPanel (Traefik),
# la app necesita saber que el visitante entró por https para que el OAuth de
# Google (redirect_uri) y los links se generen con la URL pública correcta.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
