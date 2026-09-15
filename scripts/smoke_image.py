"""
이미지를 실제로 띄워, 배포 구성이 쓰는 헬스 체크 명령을 컨테이너 안에서 그대로 돌려 본다.

테스트는 코드를 보고 이미지는 보지 않는다. 이미지에 모듈이 빠졌거나 헬스 체크가 이미지에
없는 도구를 쓰면, 테스트는 초록인데 배포만 실패한다. CI 는 이미지를 올리기 전에 이것을 돌린다.

사용: python scripts/smoke_image.py <이미지>
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICE_IMAGE = "safehome-ai-api"
CONTAINER = "safehome-ai-api-smoke"
TIMEOUT_SECONDS = 180


def healthchecks() -> dict:
    """배포 구성 파일마다 이 서비스의 헬스 체크 명령을 뽑는다."""
    found = {}
    for compose in sorted((ROOT / "docker").glob("*/docker-compose.yml")):
        image = None
        for line in compose.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s+image:\s*(\S+)", line)
            if m:
                image = m.group(1)
            m = re.match(r"^\s+test:\s*(\[.*\])\s*$", line)
            if m and image and SERVICE_IMAGE in image:
                found[compose.relative_to(ROOT).as_posix()] = json.loads(m.group(1))
    return found


def to_exec(test: list) -> list:
    if test[0] == "CMD":
        return test[1:]
    if test[0] == "CMD-SHELL":
        return ["sh", "-c", test[1]]
    raise SystemExit(f"알 수 없는 헬스 체크 형식: {test}")


def run(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    image = sys.argv[1]
    checks = healthchecks()
    if not checks:
        print("배포 구성에서 헬스 체크를 하나도 찾지 못했다. 검사할 것이 없으면 통과가 아니라 실패다.")
        return 1

    run(["docker", "rm", "-f", CONTAINER])
    # 키가 없어도 떠야 한다 — 검색 초기화 실패는 치명적으로 다루지 않는다
    started = run(["docker", "run", "-d", "--name", CONTAINER, "-e", "OPENAI_API_KEY=smoke-test", image])
    if started.returncode != 0:
        print("컨테이너를 띄우지 못했다:", started.stderr.strip())
        return 1
    try:
        pending = dict(checks)
        deadline = time.time() + TIMEOUT_SECONDS
        while pending and time.time() < deadline:
            if run(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER]).stdout.strip() != "true":
                break
            for source, test in list(pending.items()):
                if run(["docker", "exec", CONTAINER, *to_exec(test)]).returncode == 0:
                    print(f"통과: {source} 의 헬스 체크")
                    del pending[source]
            time.sleep(3)
        if not pending:
            return 0
        print("실패한 헬스 체크:")
        for source, test in pending.items():
            result = run(["docker", "exec", CONTAINER, *to_exec(test)])
            print(f"  {source}: {test}")
            print("   ", (result.stdout + result.stderr).strip()[-400:])
        logs = run(["docker", "logs", "--tail", "60", CONTAINER])
        print("컨테이너 로그 끝부분:")
        print((logs.stdout + logs.stderr).strip())
        return 1
    finally:
        run(["docker", "rm", "-f", CONTAINER])


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
