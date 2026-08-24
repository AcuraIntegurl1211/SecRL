FROM --platform=$BUILDPLATFORM node:22.14.0-alpine AS web-build
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts
COPY web/ ./
RUN npm run build

FROM python:3.11.11-slim AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements-platform.txt requirements.txt setup.py ./
RUN pip install --no-cache-dir -r requirements-platform.txt
COPY secrl_platform/ secrl_platform/
COPY secgym/ secgym/
COPY experiments/ experiments/
COPY alembic/ alembic/
COPY alembic.ini ./
RUN pip install --no-cache-dir --no-deps .
COPY docker/lite/entrypoint.sh /usr/local/bin/secrl-lite-entrypoint
COPY --from=web-build /src/web/dist /app/web/dist
RUN chmod 0755 /usr/local/bin/secrl-lite-entrypoint
RUN groupadd --system secrl && useradd --system --gid secrl --home-dir /app secrl \
    && mkdir -p /data \
    && chown -R secrl:secrl /app /data
USER secrl
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/secrl-lite-entrypoint"]
CMD ["secrl-lite", "serve", "--host", "0.0.0.0", "--port", "8080"]

FROM nginx:1.27.4-alpine AS web
COPY --from=web-build /src/web/dist /usr/share/nginx/html
COPY docker/lite/nginx.conf /etc/nginx/conf.d/default.conf
RUN touch /var/run/nginx.pid \
    && chown -R nginx:nginx /var/cache/nginx /var/run/nginx.pid /etc/nginx/conf.d /usr/share/nginx/html
USER nginx
EXPOSE 8080
