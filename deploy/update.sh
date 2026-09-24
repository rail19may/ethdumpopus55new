#!/usr/bin/env bash
# Обновление бота на сервере одной командой:
#   bash /opt/dumpbot-claude/deploy/update.sh
# Скачивает новую версию (ваши правки в config.yaml сохраняются), доустанавливает
# зависимости и перезапускает сервис.
set -euo pipefail
cd /opt/dumpbot-claude

echo "== скачиваю обновление"
git pull --ff-only --autostash -q
git log --oneline -1

echo "== зависимости"
.venv/bin/pip install -q -r requirements.txt

echo "== перезапуск"
systemctl restart dumpbot-claude
sleep 5
systemctl is-active dumpbot-claude
