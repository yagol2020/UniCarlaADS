ARG CARLA_IMAGE=carlasim/carla:0.9.10.1
FROM ${CARLA_IMAGE} AS carla

FROM nvidia/cuda:11.3.1-cudnn8-runtime-ubuntu20.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    libglib2.0-0 \
    libgl1 \
    libsm6 \
    libxext6 \
    libxrender1 \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL \
    https://repo.anaconda.com/miniconda/Miniconda3-py37_23.1.0-1-Linux-x86_64.sh \
    -o /tmp/miniconda.sh \
    && bash /tmp/miniconda.sh -b -p /opt/conda \
    && rm /tmp/miniconda.sh

ENV PATH=/opt/conda/bin:${PATH}
RUN python -m pip install --no-cache-dir --upgrade "pip<24" "setuptools<60" wheel \
    && python -m pip install --no-cache-dir \
        torch==1.10.2+cu113 \
        torchvision==0.11.3+cu113 \
        --extra-index-url https://download.pytorch.org/whl/cu113 \
    && python -m pip install --no-cache-dir \
        dictor==0.1.12 \
        easydict==1.9 \
        ephem==4.1.4 \
        networkx==2.5.1 \
        numpy==1.21.6 \
        opencv-python-headless==4.5.5.64 \
        Pillow==8.4.0 \
        pygame==2.1.2 \
        py-trees==0.8.3 \
        PyYAML==6.0.1 \
        scipy==1.7.3 \
        shapely==1.8.5.post1 \
        six==1.16.0 \
        tabulate==0.8.10

# InterFuser 的数据处理模块会在模型初始化时导入 skimage 和 imgaug。
RUN python -m pip install --no-cache-dir scikit-image==0.16.2 \
    && python -m pip install --no-cache-dir --no-deps imgaug==0.4.0

# CARLA 0.9.10 Python 扩展的运行时动态库。
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg-turbo8 \
    libpng16-16 \
    libtiff5 \
    && rm -rf /var/lib/apt/lists/*

# 从官方 CARLA 镜像取得与服务端匹配的 Python API。
COPY --from=carla /home/carla/PythonAPI /opt/carla/PythonAPI
COPY agents09101 /opt/carla_agents/agents
COPY InterFuser /opt/interfuser
COPY unicarla_ads /opt/unicarlaads/unicarla_ads

RUN CARLA_EGG=$(find /opt/carla/PythonAPI/carla/dist -name 'carla-*.egg' | head -n 1) \
    && test -n "${CARLA_EGG}" \
    && echo "${CARLA_EGG}" > /opt/conda/lib/python3.7/site-packages/carla.pth

ENV PYTHONPATH=/opt/carla_agents:/opt/carla/PythonAPI/carla:/opt/interfuser/leaderboard:/opt/interfuser/scenario_runner:/opt/interfuser/interfuser:/opt/interfuser/leaderboard/team_code:/opt/unicarlaads
ENV SDL_VIDEODRIVER=dummy
ENV UNICARLA_ADS_PORT=8080

WORKDIR /opt/interfuser
CMD ["python", "-m", "unicarla_ads.worker.server"]
