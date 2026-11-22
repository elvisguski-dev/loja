FROM python:3.12-slim
WORKDIR /app
COPY server.py index.html admin.html ./
ENV PORT=8082 LOJA_DB=/dados/loja.db TRUST_PROXY=1
VOLUME /dados
EXPOSE 8082
CMD ["python", "server.py"]
