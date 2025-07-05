FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV AUTH_TOKEN=""
ENV HOST="0.0.0.0"
ENV PORT="5500"
ENV PUSH_URL=""
EXPOSE 5500
CMD ["python", "microservice.py"]
