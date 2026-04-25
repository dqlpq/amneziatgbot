#!/bin/bash

sleep 5

cd /root/bot

screen -dmS bot  bash -c 'source /root/me/bin/activate && python bot.py'
screen -dmS webapp bash -c 'source /root/me/bin/activate && python web_service.py'
screen -dmS miniapp bash -c 'source /root/me/bin/activate && python miniapp.py'

echo "Запущено:"
echo "  bot.py     → screen -r bot"
echo "  web_service.py - screen -r webapp"
echo "  miniapp.py → screen -r miniapp"
