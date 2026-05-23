FROM python:3.12-slim-bookworm

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MINERU_MODEL_SOURCE=local \
    APP_HOST=0.0.0.0 \
    APP_PORT=5001

# 1. 安装系统级依赖 (OCR, PDF处理, 图形库)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-chi-sim \
    tesseract-ocr-eng \
    poppler-utils \
    libgl1-mesa-glx \
    libglib2.0-0 \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 2. 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. 【关键】自动修补 argostranslate 以支持离线翻译
RUN SBD_FILE=$(python -c "import argostranslate.sbd, os; print(os.path.join(os.path.dirname(argostranslate.sbd.__file__), 'sbd.py'))") && \
    sed -i '/processors="tokenize",/a\                download_method=None,' $SBD_FILE

# 4. 复制项目代码
COPY . .

# 5. 【全量交付】将模型文件直接塞入镜像
# 注意：这里使用的是相对路径，指向你刚才在项目根目录创建的 models 文件夹
COPY models/mineru/OpenDataLab /root/.cache/modelscope/hub/models/OpenDataLab
COPY models/argos-translate /root/.local/share/argos-translate
COPY mineru.json /root/mineru.json

# 6. 配置 MinerU 路径与环境变量
ENV MINERU_GLOBAL_CONFIG_PATH=/root/mineru.json
ENV MINERU_MODEL_SOURCE=local
# 强制 Hugging Face 库进入离线模式，防止将本地路径误判为 Repo ID
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1
# 强制 ModelScope 使用本地缓存，不进行联网校验
ENV MODELSCOPE_CACHE=/root/.cache/modelscope
ENV USE_MODELSCOPE_HUB=0

# 7. 创建必要的运行时目录
RUN mkdir -p .runtime uploads mineru_output

EXPOSE 5001

CMD ["python", "run.py"]
