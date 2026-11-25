#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Ошибка: build_macos.sh нужно запускать на macOS." >&2
  exit 1
fi

ARCH="$(uname -m)"
echo "==> Сборка Aufstieg.app для macOS (${ARCH})..."

if ! command -v uv >/dev/null 2>&1; then
  echo "Ошибка: не найден uv. Установите: brew install uv" >&2
  exit 1
fi

uv sync
uv run --with pyinstaller pyinstaller --noconfirm cbyd_macos.spec

APP_PATH="dist/Aufstieg.app"
if [[ ! -d "$APP_PATH" ]]; then
  echo "Ошибка: ${APP_PATH} не был создан." >&2
  exit 1
fi

echo "==> Ad-hoc подпись приложения (codesign)..."
codesign --force --deep --sign - "$APP_PATH"

DMG_NAME="dist/Aufstieg-macOS-${ARCH}.dmg"
rm -f "$DMG_NAME"
echo "==> Упаковка в ${DMG_NAME}..."
STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT
cp -R "$APP_PATH" "$STAGING/"
ln -s /Applications "$STAGING/Applications"
hdiutil create -volname "Aufstieg | Химия" -srcfolder "$STAGING" -ov -format UDZO "$DMG_NAME"

echo ""
echo "Готово!"
echo "  Приложение: ${APP_PATH}"
echo "  Образ DMG:  ${DMG_NAME}"
if [[ ! -f "cbyd.db" ]]; then
  echo "  Примечание: cbyd.db не найден в корне проекта — приложение будет скачивать синтезы онлайн"
  echo "  (или положите cbyd.db рядом с Aufstieg.app / в ~/Documents/Aufstieg/cbyd.db)."
fi
