FROM python:3.12-slim-bookworm

# ── 基础环境变量 ──────────────────────────────────────────────
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=5001

# ── 1. 安装系统级依赖 (OCR, PDF 处理, 图形库) ─────────────────
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

# ── 2. 安装项目 Python 依赖 ──────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── 3. 自动修补 argostranslate 以支持离线翻译 ─────────────────
#    argostranslate 的 sbd.py 默认会尝试联网下载 Stanza tokenizer，
#    此处注入 download_method=None 阻止联网行为。
RUN SBD_FILE=$(python -c "import argostranslate.sbd, os; print(os.path.join(os.path.dirname(argostranslate.sbd.__file__), 'sbd.py'))") && \
    sed -i '/processors="tokenize",/a\                download_method=None,' $SBD_FILE

# ── 4.【全量交付】将模型文件塞入镜像 ─────────────────────────
#    models/ 目录需在构建前手动准备（详见 docker/deploy/README-OFFLINE.md）
COPY models/mineru/OpenDataLab /root/.cache/modelscope/hub/models/OpenDataLab
COPY models/argos-translate /root/.local/share/argos-translate
COPY docker/deploy/mineru.json /root/mineru.json

# ── 4.5 修补 Stanza resources.json ────────────────────────────
# 修复 ArgosTranslate 自带的旧版 resources.json 缺失 packages 字段导致新版 Stanza 报错的问题
RUN python -c "import json, os; \
from pathlib import Path; \
pkg_dir = Path('/root/.local/share/argos-translate/packages/'); \
[p.write_text(json.dumps({k: dict(v, packages={}) if isinstance(v, dict) and 'alias' not in v and 'packages' not in v else v for k, v in json.loads(p.read_text('utf-8')).items()}), 'utf-8') for p in pkg_dir.rglob('resources.json')]"


# ── 5. 离线模式环境变量 ──────────────────────────────────────
ENV MINERU_GLOBAL_CONFIG_PATH=/root/mineru.json \
    MINERU_MODEL_SOURCE=local \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    MODELSCOPE_CACHE=/root/.cache/modelscope \
    USE_MODELSCOPE_HUB=0

# ── 6. 创建运行时目录 ────────────────────────────────────────
RUN mkdir -p runtime uploads mineru_output

EXPOSE 5001

# ── 7. 启动命令 ──────────────────────────────────────────────
#    源码通过 docker-compose.yml 的 Volume 挂载进来，
#    不在此处 COPY，便于日后更新代码时无需重建镜像。
CMD ["python", "run.py"]
