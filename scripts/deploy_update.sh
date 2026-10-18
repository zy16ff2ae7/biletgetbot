#!/bin/bash
set -e
cd /opt/bilet-bot
curl -sL https://raw.githubusercontent.com/zy16ff2ae7/biletgetbot/main/eraser_tools.py -o eraser_tools.py
curl -sL https://raw.githubusercontent.com/zy16ff2ae7/biletgetbot/main/biletnaya/server.py -o biletnaya/server.py
python3 -c "import eraser_tools; from biletnaya import server; print('Syntax check passed!')" || .venv/bin/python3 -c "import eraser_tools; from biletnaya import server; print('Syntax check passed!')"
systemctl restart bilet-bot.service
sleep 2
systemctl is-active bilet-bot.service
echo "DEPLOYMENT COMPLETE"
