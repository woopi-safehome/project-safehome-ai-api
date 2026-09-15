"""
원격 CI 결과를 본다. 로컬 통과는 중간 확인일 뿐이고, 완료는 원격 결과로 판정한다.

사용: python scripts/ci_status.py [커밋]      (기본: HEAD)
끝 코드: 0 전부 성공 / 1 실패 있음 / 2 진행 중이거나 실행 기록 없음
"""

import json
import re
import subprocess
import sys
import urllib.request


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()


def get(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "ci-status"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def main() -> int:
    sha = git("rev-parse", sys.argv[1] if len(sys.argv) > 1 else "HEAD")
    remote = git("remote", "get-url", "origin")
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", remote)
    if not m:
        print(f"GitHub 원격이 아니다: {remote}")
        return 2
    repo = m.group(1)
    runs = get(f"https://api.github.com/repos/{repo}/actions/runs?head_sha={sha}&per_page=20").get("workflow_runs", [])
    print(f"{repo} @ {sha[:7]}")
    if not runs:
        print("  실행 기록 없음 — 아직 푸시되지 않았거나, 이 커밋이 워크플로 조건에 해당하지 않는다")
        return 2
    failed = pending = False
    for run in runs:
        print(f"  {run['name']}: {run['status']}/{run['conclusion']}  {run['html_url']}")
        if run["status"] != "completed":
            pending = True
        elif run["conclusion"] != "success":
            failed = True
            for job in get(run["jobs_url"]).get("jobs", []):
                if job.get("conclusion") in ("success", "skipped"):
                    continue
                steps = [s["name"] for s in job.get("steps", []) if s.get("conclusion") == "failure"]
                print(f"    {job['name']}: {job['conclusion']}  실패 단계: {', '.join(steps) or '-'}")
    return 1 if failed else 2 if pending else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
