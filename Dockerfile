FROM python:3.11-slim

WORKDIR /app

# 安装依赖（利用 Docker 层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY . .

# 数据目录（stats.db / relay.log）挂载点
VOLUME ["/app/app/data"]

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
