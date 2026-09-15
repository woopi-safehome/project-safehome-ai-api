#!/usr/bin/env bash
# 이 저장소의 검증. CI 의 테스트 단계가 이 파일을 그대로 실행한다.
# 끝났다고 말하기 전에도 이것을 돌린다 — 명령이 다르면 로컬 통과가 CI 통과를 뜻하지 않는다.
set -euo pipefail
cd "$(dirname "$0")/.."
python -m pytest tests/ -q
