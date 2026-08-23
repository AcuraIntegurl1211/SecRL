FROM python:3.11.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements-platform.txt requirements.txt setup.py ./
RUN pip install --no-cache-dir -r requirements-platform.txt
COPY secrl_platform/ secrl_platform/
COPY examples/ examples/
RUN pip install --no-cache-dir --no-deps .
RUN groupadd --system agent && useradd --system --gid agent --home-dir /app agent
USER agent
EXPOSE 8081
CMD ["uvicorn", "examples.agent_service.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8081"]
