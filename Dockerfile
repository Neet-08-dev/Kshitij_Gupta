FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY . /app
USER 65532:65532
CMD ["python", "-m", "starter.agent", "--host", "0.0.0.0"]
