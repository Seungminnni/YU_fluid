#!/bin/zsh
set -e
cd "$(dirname "$0")"
if ! command -v python3.12 >/dev/null 2>&1; then
  echo "Python 3.12 이상을 설치한 뒤 다시 실행하세요."
  read -r "?Enter를 누르면 닫습니다. "
  exit 1
fi
if [[ ! -f .venv/bin/python ]]; then python3.12 -m venv .venv; fi
.venv/bin/python -m pip install -e '.[cad,results,media]'
echo "웹앱: http://127.0.0.1:8765/"
echo "이 창을 열어 둔 채 브라우저에서 위 주소로 접속하세요."
exec .venv/bin/python -m cfd_twin serve-workflow \
  --case-dir data/case --cad-dir data/cad --viewer-dir viewer --port 8765
