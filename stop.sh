#!/bin/bash
screen -S bot  -X quit
screen -S miniapp -X quit
screen -S webapp -X quit
echo "Остановлено"
