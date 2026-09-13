# 第三方许可声明

本项目自身代码采用 [MIT License](LICENSE)。以下第三方组件在源码、Docker 镜像或运行过程中被使用，
其版权归原作者所有，遵循各自的原始许可。

## InterFuser

- 来源：<https://github.com/opendilab/InterFuser>
- 许可：Apache License 2.0（完整文本见上游 `LICENSE`）
- 版本：submodule 固定在上游 commit `f0be8ea`
- 说明：预训练权重 `interfuser.pth.tar` 不在仓库内，默认从本仓库的 GitHub Release
  （`v0.1`）下载，脚本为 `scripts/download_interfuser_weights.sh`；其内置的 CARLA
  leaderboard 与 scenario_runner 为 MIT。
- 本项目未修改其源码，仅通过外部适配器在运行时调用；构建出的镜像内保留了上游许可文件。

## LEAD

- 来源：<https://github.com/kesai-labs/lead>
- 许可：MIT，Copyright (c) 2025 Long Nguyen
- 版本：submodule 固定在上游 tag `v1.5.0`（commit `197fb5dd`）
- 检查点：<https://huggingface.co/ln2697/transfuser-carla-123d>，MIT，不在 git 仓库内，
  需通过 `scripts/download_lead_checkpoint.sh` 下载
- 说明：其 `3rd_party` 内置的 CARLA leaderboard 与 scenario_runner 为 MIT。

## CARLA 及其组件

- CARLA Simulator / Python API：MIT，Copyright (c) 2017 Computer Vision Center (CVC) at the
  Universitat Autonoma de Barcelona (UAB)，<https://github.com/carla-simulator/carla>
- CARLA Agents（`agents09101`、`agents0913`、`agents0915`、`agents0916`）：来自 CARLA
  leaderboard，MIT，Copyright (c) 2019 CARLA
- Leaderboard / ScenarioRunner：MIT，Copyright (c) 2019 / 2018 CARLA

## Autoware 与 autoware_carla_launch

- autoware_carla_launch：Apache License 2.0，
  <https://github.com/evshary/autoware_carla_launch>
  - 版本：submodule 固定在上游 commit `86628e9`
  - 其嵌套子模块：`zenoh_carla_bridge`（commit `7975212`，Apache-2.0）、
    `zenoh-plugin-ros2dds`（Apache-2.0 / EPL-2.0）、`autoware_manual_control`（Apache-2.0）
- Autoware：Apache License 2.0，<https://github.com/autowarefoundation/autoware>
  - 基础镜像：`ghcr.io/autowarefoundation/autoware:universe-devel-cuda-jazzy-1.8.0`
  - autoware_carla_launch 发布的 Town01 高精度地图与模型权重不在 git 仓库内，
    通过上游脚本 `scripts/download_autoware_assets.sh` 下载，遵循上游数据许可。
- 本项目对 `zenoh_carla_bridge` 的修改（`autoware_overlay/`）属于本项目的 MIT 代码，
  在构建镜像时覆盖上游文件，submodule 内容保持与上游一致。

## 基础镜像

`carlasim/carla`、`nvidia/cuda`、`pytorch/pytorch` 等基础镜像由 Docker Hub 拉取，
遵循各自镜像的许可与使用条款。

## MIT License

以下 MIT 组件均适用此许可文本（版权人见上）：

```text
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
