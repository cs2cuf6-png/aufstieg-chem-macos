# Aufstieg | Химия (cbyd-flashcards)

Автономный CLI/TUI-инструмент для поиска и генерации самодостаточных офлайн HTML-флешкарт (и PDF) по синтезам из Chemistry By Design.

## Возможности

- Интерактивный TUI-интерфейс с поиском по базе синтезов и превью молекулы
- Генерация самодостаточных офлайн HTML-флешкарт («змейка») с открытием в нативном окне (WebView2 на Windows / WKWebView на macOS)
- Экспорт в PDF (`Ctrl+P`) и открытие PDF (`Ctrl+O`)
- Случайный синтез (`Ctrl+R`), очистка поиска (`Ctrl+Z`), контакты (`Ctrl+A`)

## Запуск

### Windows — готовый `cbyd.exe`
Скачайте `cbyd.exe` из раздела **Releases** и запустите двойным кликом. База `cbyd.db` и `contacts.html` уже вшиты внутрь.

### macOS — готовый установщик `.pkg`
1. Скачайте `Aufstieg-macOS-Installer-x86_64.pkg` из **Releases** и запустите его (при первом запуске неподписанного установщика: правый клик → **Открыть**).
2. Пройдите стандартный мастер установки — `Aufstieg.app` автоматически установится в **Программы (Applications)**, снимет карантин `xattr` и при необходимости подтянет Rosetta 2 на Apple Silicon (M1–M4).
3. После установки приложение запускается обычным двойным кликом из **Программ / Launchpad**. Готовые флешкарты и PDF сохраняются в `~/Documents/Aufstieg/flashcards`. Для экспорта в PDF (`Ctrl+P`) нужен установленный Chrome, Edge, Brave, Arc или Chromium.

### Из исходников (Windows и macOS)
Требуется Python 3.11+ и `uv`:
```bash
uv sync
uv run python cbyd_entry.py
```
На macOS также можно запускать двойным кликом файл `start_macos.command` (предварительно сделав `chmod +x start_macos.command`).

## Сборка

### Windows (`cbyd.exe`)
```bash
uv run pyinstaller --noconfirm cbyd.spec
```
(Для сборки требуется файл `cbyd.db` в корне проекта.)

### macOS (`Aufstieg.app` и `.dmg`)
Напрямую на Mac:
```bash
chmod +x build_macos.sh
./build_macos.sh
```
Если в корне проекта лежит `cbyd.db`, она автоматически вошьётся внутрь `Aufstieg.app`.

Также в репозитории есть GitHub Actions workflow `.github/workflows/build-macos.yml` (запуск вручную через **Actions → Build macOS App → Run workflow**), который собирает `.dmg` для `arm64` и `x86_64` в облаке (с опциональным параметром `db_url` для вшивания `cbyd.db`).
