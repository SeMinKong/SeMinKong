# 공세민 | Se Min Kong

안녕하세요, 공세민입니다.

카메라와 모델이 만든 결과를 실제 장치의 움직임까지 연결하는 작업을 좋아합니다.  
숭실대학교에서 소프트웨어를 전공했고, 현재 SSAFY Robotics Track에서 ROS 2와 로봇 시스템을 공부하고 있습니다.

로봇을 만들 때는 잘 움직이는지만 보지 않습니다. 센서 연결이 끊겼을 때 안전하게 멈추는지, 여러 명령이 겹치면 무엇을 먼저 처리할지, 실험 과정을 나중에 다시 확인할 수 있는지도 함께 봅니다.

[포트폴리오](https://seminkong.github.io/SeMinKong_Web/) · [이력서](https://seminkong.github.io/SeMinKong_Web/resume/) · [메일](mailto:semin1224@gmail.com)

## 요즘 보고 있는 것

- 손의 21개 landmark를 7축 텐던 핸드 명령으로 바꾸는 방법
- simulator에서 잘 되던 움직임이 실제 장치에서도 버티게 만드는 방법
- Jetson과 Raspberry Pi에서 지연 시간, 메모리, 기록을 함께 챙기는 방법

## 최근에 만든 것

### THING

사람의 손동작을 따라 하는 7축 텐던 로봇 핸드를 만든 팀 프로젝트입니다.  
MediaPipe로 읽은 손 좌표를 실제 모터 명령으로 바꾸고, ROS 2에서 명령 중재와 안전 처리를 거쳐 로봇 손까지 전달했습니다.

구성 메모:

- 입력: 카메라와 MediaPipe의 21개 hand landmark
- 명령 생성: Jetson에서 7축 `HandCommand` 생성
- 안전 처리: Raspberry Pi 5, ROS 2 명령 우선순위, 관절 제한, GPIO E-stop
- 구동: DYNAMIXEL XL330 7개와 텐던 구조
- 기록: rosbag2 데이터를 JSON·CSV로 변환하고 SHA-256과 함께 EC2에 저장

<p align="center">
  <a href="https://seminkong.github.io/SeMinKong_Web/work/thing/">
    <img src="https://raw.githubusercontent.com/SeMinKong/SeMinKong_Web/main/src/assets/projects/thing/final-demo-poster.webp" width="640" alt="THING 텐던 로봇 핸드 시연 포스터" />
  </a>
</p>

[프로젝트 기록](https://seminkong.github.io/SeMinKong_Web/work/thing/) · [코드](https://github.com/SeMinKong/THING) · [시연 영상](https://github.com/SeMinKong/THING#시연)

### 다른 작업

- [**AQIS**](https://seminkong.github.io/SeMinKong_Web/work/aqis/) — 팀장으로 참여한 스마트 팩토리 검사 프로젝트입니다. RealSense와 YOLO의 검사 결과를 컨베이어, Dobot, 대시보드까지 연결했습니다.
- [**Brain MRI**](https://seminkong.github.io/SeMinKong_Web/work/brain-tumor-mri/) — YOLO11로 뇌 MRI 분류와 segmentation을 함께 실험했습니다. [전처리와 학습 코드](https://github.com/SeMinKong/BrainMRISegmentation_YOLO)도 정리해 두었습니다.
- [**Project Prompt Generator**](https://seminkong.github.io/SeMinKong_Web/work/project-prompt-generator/) — 하나의 아이디어를 여러 설계 대화로 나누는 [LangGraph workflow](https://github.com/SeMinKong/ProjectPromptGenerator_LangGraph)입니다. FastAPI와 WebSocket으로 진행 상황을 보여줍니다.
- [**Briefit**](https://seminkong.github.io/SeMinKong_Web/work/briefit/) — 뉴스를 비동기로 모으고, 비슷한 기사를 묶은 뒤 KoBART로 요약하는 파이프라인을 만들었습니다.

공부하면서 만든 작은 실험은 [Snake DQN](https://github.com/SeMinKong/Snake_DQN)과 [TSP GPU Solver](https://github.com/SeMinKong/TSP)에 남겨 두었습니다.

## 자주 쓰는 도구

로봇 쪽에서는 ROS 2, Python, C++, DYNAMIXEL을 주로 씁니다.  
비전 작업에는 OpenCV, MediaPipe, PyTorch, YOLO를 사용하고, 필요한 API와 화면은 FastAPI, React, WebSocket으로 붙입니다.

최근에는 Isaac Sim/Lab과 edge inference를 공부하고 있습니다.

## 간단한 이력

- 2026–현재 · SSAFY Robotics Track
- 2020–2026 · 숭실대학교 소프트웨어학부, AI·빅데이터 융합전공

## GitHub 기록

잔디는 평면보다 지형으로 보는 편이 재미있어서 3D로 남겨 두었습니다. GitHub Actions가 하루에 한 번 갱신합니다.

<p align="center">
  <img src="https://raw.githubusercontent.com/SeMinKong/SeMinKong/main/profile-3d-contrib/profile-physical-ai-static.svg" width="100%" alt="공세민의 GitHub 활동을 나타낸 3D 기여 지형" />
</p>

작업 이야기는 [메일](mailto:semin1224@gmail.com)로 편하게 연락 주세요.

