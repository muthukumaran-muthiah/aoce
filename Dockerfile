FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
# Run as an unprivileged user (AOCE-66). Numeric UID so Kubernetes runAsNonRoot can verify it.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin aoce
USER 10001
EXPOSE 8000
CMD ["uvicorn", "aoce.app:app", "--host", "0.0.0.0", "--port", "8000"]
