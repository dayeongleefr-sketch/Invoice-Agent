# 물류비 검증 — 설치 없이 사용하는 공개 웹 버전

**공개 실행 주소:** [https://invoice-agent-demo-bj7i.onrender.com](https://invoice-agent-demo-bj7i.onrender.com)

2026-11-20 Render **Invoice agent** 작업 공간의 **Free** Docker 서비스에 배포했습니다.
로그인·PC 설치 없이 포함된 가상 자료를 실제 Python으로 계산하고 결과를 조회합니다.
AI 호출과 방문자 파일 업로드는 사용하지 않습니다. 공개 HTTPS에서 재계산·원본·보고서 조회를 확인했습니다.
실제 배포 근거는 [DEPLOYMENT.json](DEPLOYMENT.json)입니다.

현재 서비스는 자동 배포를 꺼 두었습니다. 소스를 변경한 뒤 Render 서비스의 **Manual Deploy → Deploy latest commit**으로 반영합니다.
서버가 쉬고 있으면 첫 접속에 약 1분이 걸릴 수 있습니다. 기존 `logistics_agent_full.zip`은 별도 PC 설치용입니다.

GitHub는 코드를 보관하고, Render Web Service가 실제 Python 계산과 웹 화면을 실행합니다.
배포 완료 후 발급된 HTTPS 주소를 공유하면 방문자는 Windows/Mac 구분 없이 브라우저로 사용합니다.
이 폴더는 GitHub 저장소 루트에 올릴 소스입니다. ZIP 파일만 저장소에 올리면 Render가 실행할 수 없습니다.

## 공개 범위

- 독립 가상 청구·WMS를 Python으로 실제 계산합니다. 정적 화면 재생이나 AI 응답 흉내가 아닙니다.
- 기존 대시보드·확인 목록·상세·원본 미리보기·보고서를 조회하고 **가상 자료 다시 계산**으로 재계산합니다.
- AI 호출과 AI 로그인은 사용하지 않습니다. 모델 호출 경로도 서버에서 차단합니다.
- 방문자는 같은 고정 가상 자료를 조회합니다. 파일 업로드·담당자 결정 저장·기준 변경은 공개판에서 제공하지 않습니다.
- 중복 실행은 한 작업으로 제한하고 30초 간격으로 다시 계산합니다. 자료 차이와 미확정 항목은 유지합니다.
- 실제 고객 자료·기존 DB·승인·인증 파일은 포함하지 않습니다. 로컬 전체 실행본과 기존 정적 HTTPS 데모는 별도입니다.

## GitHub → Render 배포

1. 새 GitHub 저장소에 이 폴더 **안의 파일과 폴더**를 등록합니다. 루트에 Dockerfile, render.yaml, server.py가 있어야 합니다.
   파일이 100개를 넘으므로 GitHub Desktop/CLI 또는 연결된 GitHub 기능을 사용하세요.
2. Render에 로그인해 GitHub 저장소를 연결합니다. New → Blueprint로 render.yaml을 사용하거나 New → Web Service에서 저장소를 선택합니다.
3. Web Service 방식은 Language **Docker**, Dockerfile Path **./Dockerfile**, Plan **Free**, Health Check **/healthz**입니다.
   API 키·Codex 인증은 입력하지 않습니다. 유료 플랜으로 자동 변경하지 않습니다.
4. 배포 성공 후 Render가 표시한 `https://서비스명.onrender.com` 주소로 접속합니다. GitHub 저장소 주소가 실행 주소는 아닙니다.
5. 대시보드 → 다시 계산 → 확인 목록 → 상세/원본 → 보고서를 확인한 뒤 링크를 공유합니다.

서버 시작 시 가상 자료를 계산합니다. Free 플랜은 유휴 시 중단되고 다시 접속할 때 준비 시간이 들며,
재시작/재배포 시 로컬 파일이 사라집니다. 이 버전은 고정 가상 자료를 재생성하므로 개인 작업 기록 보관용이 아닙니다.
무료 제공량·계정 제한은 배포 시 Render 화면에서 확인합니다. 공개 URL 발급 전까지 배포 완료로 표시하지 않습니다.

공식 안내: [Render Docker](https://render.com/docs/docker), [Web Services](https://render.com/docs/web-services),
[무료 플랜](https://render.com/docs/free), [Blueprint](https://render.com/docs/infrastructure-as-code).

## 다른 서버

Docker 컨테이너의 PORT(기본 10000)를 HTTPS 역방향 프록시에 연결하고 PUBLIC_HOST에 실제 도메인을 지정합니다.
Render는 RENDER_EXTERNAL_HOSTNAME을 자동 제공합니다. 한 프로세스·한 인스턴스로 실행합니다.
사용자별 파일 검증과 영구 저장이 필요하면 인증·저장소 분리·접근 통제를 추가해야 합니다.

## 검증 상태

VALIDATION.json에서 로컬 공개 모드·실제 계산·AI 차단·브라우저·다운로드 범위·호스트/Origin 차단을 구분합니다.
로컬 시험과 실제 Linux Docker/Render HTTPS 검증을 구분하며, 실제 공개 서비스 결과는 DEPLOYMENT.json에 기록합니다.
