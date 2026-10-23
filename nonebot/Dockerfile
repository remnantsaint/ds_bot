FROM python:3.11-slim

ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --timeout 120 --retries 5 -r requirements.txt

COPY pyproject.toml bot.py ./
COPY src ./src

# Compose 运行时只读挂载代码和配置；独立镜像也保留业务代码。
CMD ["python", "bot.py"]
