# docker — 컨테이너 배포 설정

> **범위**: `docker/**` + `Dockerfile`
> **상위**: [AI API README](../README.md)
> **검증**: 환경변수 목록은 `.env.template`, 배포 단계는 `.github/workflows/deploy-ai-api-*.yml`과 대조

## 디렉토리 구조

```
docker/
├── dev/
│   ├── docker-compose.yml   # 개발 서버 배포 구성
│   └── .env.template        # 개발 서버 환경변수 템플릿
└── prd/
    ├── docker-compose.yml   # 운영 서버 배포 구성
    └── .env.template        # 운영 서버 환경변수 템플릿
```

## 환경변수 (.env.template)

서버에서 실제 `.env` 파일로 복사하여 값을 채운 뒤 사용한다.
GitHub Actions는 이 파일을 직접 사용하지 않고, **서버에 이미 존재하는 env 파일**을 참조한다.

| 변수 | 설명 | 필수 |
|------|------|:---:|
| `GITHUB_OWNER` | GitHub Container Registry 소유자 (이미지 pull용) | ✅ |
| `OPENAI_API_KEY` | OpenAI API 키 | ✅ |
| `SENTRY_DSN` | Sentry DSN (미설정 시 Sentry 비활성) | - |
| `RAG_UPDATE_HOUR` | RAG 자동 업데이트 실행 시각 (0~23, 기본 3시) | - |
| `LAW_API_KEY` | 국가법령정보 오픈API 키 (법령 자동 업데이트용) | - |

## 서버 초기 설정

```bash
# 서버에서 실행
mkdir -p /home/woopi/project/safehome/env

# AI API env 파일 생성
cp docker/dev/.env.template /home/woopi/project/safehome/env/.env_ai_api
vi /home/woopi/project/safehome/env/.env_ai_api   # 실제 값 입력
```

## 배포 흐름

GitHub Actions (`deploy-ai-api-dev.yml`)가 자동으로 처리:

```
1. Docker 이미지 빌드 → GHCR(ghcr.io) push
2. docker-compose.yml을 서버로 SCP 전송
3. SSH 접속 → docker compose --env-file .env_ai_api up -d
```

수동 배포가 필요한 경우:

```bash
# 서버에서 직접 실행
cd /home/woopi/project/safehome/ai-api
docker compose --env-file /home/woopi/project/safehome/env/.env_ai_api pull safehome-ai-api
docker compose --env-file /home/woopi/project/safehome/env/.env_ai_api up -d --no-deps safehome-ai-api
docker image prune -f
```

## 이미지 태그 전략

| 환경 | 태그 |
|------|------|
| dev | `dev`, `dev-{git short sha}` |
| prd | `latest`, `{git tag}` |
