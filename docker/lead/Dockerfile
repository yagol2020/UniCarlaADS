FROM pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    libgl1 \
    libglib2.0-0 \
    libturbojpeg \
    && rm -rf /var/lib/apt/lists/*

# 与 LEAD 声明的 PyTorch 2.8 环境保持一致。
RUN python -m pip install --no-cache-dir \
    "torchvision==0.23.0" \
    --index-url https://download.pytorch.org/whl/cu128

RUN python -m pip install --no-cache-dir \
    "setuptools==81" \
    "carla==0.9.16" \
    "beartype==0.21" \
    "filterpy==1.4.5" \
    "huggingface-hub[hf_xet]==0.36.2" \
    "imagecodecs==2025.3.30" \
    "jaxtyping==0.3.2" \
    "lightning==2.6.1" \
    "lmdb>=1.4,<2" \
    "matplotlib==3.10.3" \
    "numba==0.61.2" \
    "numpy==1.26.4" \
    "omegaconf==2.3.0" \
    "opencv-python-headless==4.11.0.86" \
    "pillow==10.2.0" \
    "psutil==5.9.8" \
    "py-trees==0.8.3" \
    "pyquaternion==0.9.9" \
    "pyturbojpeg==2.5" \
    "PyYAML==6.0.2" \
    "scipy==1.14.1" \
    "shapely==2.0.4" \
    "six==1.16.0" \
    "tabulate==0.9.0" \
    "timm==1.0.25" \
    "torchmetrics==1.9.0" \
    "tqdm==4.66.1" \
    "trimesh>=4,<5" \
    "transforms3d==0.4.1" \
    "typing-extensions>=4.12,<5" \
    "xmlschema==1.0.18"

RUN python -m pip install --no-cache-dir \
    "cachetools==7.1.8" \
    "DracoPy==2.0.0" \
    "laspy[lazrs]==2.7.0" \
    "pandas==3.0.5" \
    "pyarrow==25.0.1" \
    "ray==2.58.0"

# 只安装 py123d 本体；上面已补齐在线推理导入链所需依赖。
RUN python -m pip install --no-cache-dir --no-deps \
    "py123d @ https://codeload.github.com/kesai-labs/py123d/tar.gz/4d20a6cfd50f7966647448084f6482c0b65effff"

COPY lead /opt/lead
COPY unicarla_ads /opt/unicarlaads/unicarla_ads

RUN python -m pip install --no-cache-dir --no-deps /opt/lead

ENV PYTHONPATH=/opt/lead/3rd_party/CARLA/standard_0916/PythonAPI/carla:/opt/lead/3rd_party/leaderboard/standard/leaderboard:/opt/lead/3rd_party/leaderboard/standard/scenario_runner:/opt/unicarlaads
ENV UNICARLA_ADS_NAME=lead
ENV UNICARLA_ADS_PORT=8080
ENV LEAD_RUNTIME_TYPE_CHECKING=false
ENV MPLBACKEND=Agg

WORKDIR /opt/lead
CMD ["python", "-m", "unicarla_ads.worker.server"]
