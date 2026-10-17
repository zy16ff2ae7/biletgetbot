#!/bin/bash
# Бот постоянно работает на VDS. Локальный второй polling не запускается.
if [ "${1:-}" = "--deploy" ]; then
    exec bash "$(dirname "$0")/deploy-vds.sh"
fi
printf 'Бот работает на VDS: https://t.me/biletgetbot\nДля обновления запустите этот файл с параметром --deploy.\n'
open 'https://t.me/biletgetbot'
