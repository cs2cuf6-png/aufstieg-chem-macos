#!/usr/bin/env bash
cd "$(dirname "$0")"
printf '\033]0;Aufstieg | Химия\007'

if [[ -x ".venv/bin/cbyd" ]]; then
  ".venv/bin/cbyd" "$@"
elif command -v uv >/dev/null 2>&1; then
  uv run python cbyd_entry.py "$@"
else
  echo "Не найден uv или .venv. Установите uv: brew install uv"
  read -r -p "Нажмите Enter для выхода..."
  exit 1
fi

status=$?
if [[ $status -ne 0 ]]; then
  echo ""
  read -r -p "Произошла ошибка (код $status). Нажмите Enter для выхода..."
fi
