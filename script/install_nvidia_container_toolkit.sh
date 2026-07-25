#!/usr/bin/env bash
# 安装 / 配置 NVIDIA Container Toolkit，使 docker --gpus 可用。
# 需要 sudo。装完后请重建并重启:
#   ./script/run_local_ui.sh start-server docker --build

set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }

command -v nvidia-smi >/dev/null 2>&1 || die "主机未见 nvidia-smi，请先安装 NVIDIA 驱动"
command -v docker >/dev/null 2>&1 || die "未找到 docker"

echo "==> 添加 NVIDIA Container Toolkit apt 源"
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null

echo "==> apt install nvidia-container-toolkit"
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit

echo "==> 配置 Docker nvidia runtime"
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

echo "==> 验证 --gpus device=0"
docker run --rm --gpus device=0 ubuntu:24.04 nvidia-smi -L \
  || docker run --rm --gpus device=0 openeuler-workflow:latest \
       python3 -c 'import os; print("ok devices", os.listdir("/dev")); print("NVIDIA_VISIBLE_DEVICES", os.environ.get("NVIDIA_VISIBLE_DEVICES"))'

echo "ok: nvidia-container-toolkit ready"
echo "next: ./script/run_local_ui.sh start-server docker --build"
