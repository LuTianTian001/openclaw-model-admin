# 需能执行 openclaw config validate 时，请在镜像内安装 openclaw CLI，或挂载宿主机的 openclaw，
# 或设置环境变量 OPENCLAW_MODEL_ADMIN_SKIP_VALIDATE=1（仅建议开发/测试）。
FROM python:3.12-slim
WORKDIR /app
COPY server.py ./
COPY static ./static/
ENV OPENCLAW_MODEL_ADMIN_HOST=0.0.0.0
EXPOSE 8765
CMD ["python3", "server.py"]
