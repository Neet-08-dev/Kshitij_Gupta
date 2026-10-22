FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY . /app
# Site timezones need the IANA database; slim images may omit it. Best effort: without it the agent asks for UTC
# times and treats access windows in unknown zones as closed, so a failed install degrades safely.
RUN (apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*) \
    || echo "tzdata not installed; non-UTC site zones will be handled conservatively"
USER 65532:65532
CMD ["python", "-m", "starter.agent", "--host", "0.0.0.0"]
