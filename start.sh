#!/bin/bash

sleep 5

cd /root/bot

screen -dmS bot  bash -c 'source /root/me/bin/activate && python bot.py'
screen -dmS miniapp bash -c 'source /root/me/bin/activate && python miniapp.py'

echo "Запущено:"
echo "  bot.py     → screen -r bot"
echo "  miniapp.py → screen -r miniapp"
