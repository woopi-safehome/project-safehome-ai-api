@echo off
:: SafeHome RAG Daily Updater
:: Windows 작업 스케줄러에 등록하여 매일 자동 실행
::
:: [작업 스케줄러 등록 방법]
:: 1. "작업 스케줄러" 열기 (Win+S → "작업 스케줄러")
:: 2. "기본 작업 만들기" 클릭
:: 3. 트리거: 매일 오전 9:00 (원하는 시간으로 설정)
:: 4. 동작: "프로그램 시작" → 이 .bat 파일 경로 지정

cd /d "%~dp0"

:: Python 경로를 직접 지정하거나 가상환경 활성화
:: 가상환경 사용 시 아래 주석 해제 후 경로 수정
:: call .venv\Scripts\activate.bat

python rag\updater.py >> logs\scheduler.log 2>&1

echo.
echo [완료] logs\update_%DATE:~0,4%%DATE:~5,2%%DATE:~8,2%.log 를 확인하세요.
echo 검토 후 적용: python rag\apply_pending.py data\pending_%DATE:~0,4%%DATE:~5,2%%DATE:~8,2%.json
