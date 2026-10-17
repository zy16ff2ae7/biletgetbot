#!/bin/bash
# Update only @biletgetbot; preserve credentials and subscription receipts.
set -euo pipefail
bilet_root=$(cd "$(dirname "$0")" && pwd)
bilet_editor=${BILET_EDITOR:-/Users/mac/Билетная}
bilet_work=${BILET_DEPLOY_WORK:-"$PWD/work/bilet-deploy"}
mkdir -p "$bilet_work"
bilet_package=$(mktemp -d "$bilet_work/package.XXXXXX")
trap 'rm -rf "$bilet_package"' EXIT
mkdir -p "$bilet_package/biletnaya" "$bilet_package/tests"
cp "$bilet_root/"{bot.py,batches.py,ticket_meta.py,seat_tools.py,eraser_tools.py,validation.py,progress.py,delivery_cache.py,url_delivery.py,telegram_file_transport.py,nginx-bilet-delivery.conf,subscriptions.py,subscription.json,requirements.txt,README.md,make_brand.py,LICENSE,config.py,texts.py} "$bilet_package/"
cp -R "$bilet_root/migrations" "$bilet_package/"
cp "$bilet_root/tests/"test_*.py "$bilet_package/tests/"
if [ -f "$bilet_root/tests/_stub_engine.py" ]; then cp "$bilet_root/tests/_stub_engine.py" "$bilet_package/tests/"; fi
if [ -d "$bilet_root/scripts" ]; then cp -R "$bilet_root/scripts" "$bilet_package/"; fi
for bilet_unit in bilet-bot.service bilet-backup.service bilet-backup.timer; do
    if [ -f "$bilet_root/$bilet_unit" ]; then cp "$bilet_root/$bilet_unit" "$bilet_package/"; fi
done
cp "$bilet_editor/"{server.py,raster.py,requirements.txt,index.html} "$bilet_package/biletnaya/"
cp -R "$bilet_editor/vendor" "$bilet_editor/tests" "$bilet_package/biletnaya/"
if [ -d "$bilet_root/brand" ]; then cp -R "$bilet_root/brand" "$bilet_package/"; fi
COPYFILE_DISABLE=1 tar -czf "$bilet_work/package.tgz" -C "$bilet_package" .
bilet_ssh=(ssh -o BatchMode=yes -o ConnectTimeout=45)
bilet_scp=(scp -o BatchMode=yes -o ConnectTimeout=45)
if [ -n "${BILET_SSH_CONTROL:-}" ]; then
    bilet_ssh+=(-o "ControlPath=$BILET_SSH_CONTROL")
    bilet_scp+=(-o "ControlPath=$BILET_SSH_CONTROL")
fi
bilet_stage=$("${bilet_ssh[@]}" theatra1ka 'umask 077; mktemp -d /opt/bilet-bot-stage.XXXXXX')
"${bilet_scp[@]}" "$bilet_work/package.tgz" "theatra1ka:$bilet_stage/package.tgz"
"${bilet_ssh[@]}" theatra1ka bash -s -- "$bilet_stage" "${BILET_DEPLOY_SETTINGS:-0}" "${BILET_GATEWAY_ONLY:-0}" "${BILET_ENABLE_URL_DELIVERY:-0}" <<'REMOTE'
set -euo pipefail
bilet_stage=$1
bilet_settings=$2
bilet_gateway_only=$3
bilet_enable_url=$4
case "$bilet_stage" in /opt/bilet-bot-stage.*) ;; *) exit 2 ;; esac
bilet_root=/opt/bilet-bot
bilet_python=$bilet_root/.venv/bin/python
umask 077
tar -xzf "$bilet_stage/package.tgz" -C "$bilet_stage"
"$bilet_python" -m pip install -r "$bilet_stage/requirements.txt"
cd "$bilet_stage"
BILETNAYA="$bilet_stage/biletnaya" BILET_STATE_DIR="$bilet_stage/test-state" "$bilet_python" -m unittest discover -s tests -v
"$bilet_python" biletnaya/tests/autoclean_synth.py
bilet_backup="$bilet_root/.deploy-backups/$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$bilet_backup/code"
if [ "$bilet_gateway_only" = 1 ]; then
    mkdir -p "$bilet_backup/gateway"
    bilet_nginx=/etc/nginx/sites-available/theatralka
    bilet_snippet=/etc/nginx/snippets/bilet-delivery.conf
    cp -a "$bilet_nginx" "$bilet_backup/gateway/site"
    if [ -f "$bilet_snippet" ]; then cp -a "$bilet_snippet" "$bilet_backup/gateway/snippet"; fi
    bilet_gateway_rollback() {
        trap - ERR
        cp -a "$bilet_backup/gateway/site" "$bilet_nginx"
        if [ -f "$bilet_backup/gateway/snippet" ]; then
            cp -a "$bilet_backup/gateway/snippet" "$bilet_snippet"
        else
            rm -f "$bilet_snippet"
        fi
        nginx -t && systemctl reload nginx
        printf 'GATEWAY ROLLBACK; backup=%s\n' "$bilet_backup" >&2
        exit 1
    }
    trap bilet_gateway_rollback ERR
    cp "$bilet_stage/nginx-bilet-delivery.conf" "$bilet_snippet"
    "$bilet_python" - "$bilet_nginx" <<'NGINX'
from pathlib import Path
import sys
path = Path(sys.argv[1])
assert Path('/etc/nginx/sites-enabled/theatralka').resolve() == path
text = path.read_text()
include = '    include /etc/nginx/snippets/bilet-delivery.conf;'
if include not in text:
    anchor = '    server_name theatralka.ru;'
    assert text.count(anchor) == 1
    path.write_text(text.replace(anchor, anchor + '\n' + include, 1))
NGINX
    nginx -t
    systemctl reload nginx
    # Test the exact public HTTPS path with temporary memory-only bytes.
    "$bilet_python" - "$bilet_stage" <<'PREFLIGHT'
import sys, hashlib
sys.path.insert(0, sys.argv[1])
import requests
from url_delivery import Publisher
data = b'%PDF-1.4\nGateway preflight (not a ticket)\n%%EOF\n'
with Publisher('https://theatralka.ru/-bilet-download') as publisher:
    with publisher.publish(data, 'preflight.pdf', ttl=120) as lease:
        response = requests.get(lease.url, timeout=(5, 15))
        assert response.status_code == 200, response.status_code
        assert response.content == data
        assert 'no-store' in response.headers.get('Cache-Control', '')
        print('Private HTTPS gateway: exact bytes and no-store verified')
PREFLIGHT
    mkdir -p "$bilet_root/http-candidate"
    cp "$bilet_stage/url_delivery.py" "$bilet_root/http-candidate/"
    trap - ERR
    rm -rf "$bilet_stage"
    printf 'GATEWAY READY; candidate=%s; backup=%s\n' "$bilet_root/http-candidate" "$bilet_backup"
    exit 0
fi
for bilet_file in bot.py batches.py ticket_meta.py seat_tools.py eraser_tools.py validation.py progress.py delivery_cache.py url_delivery.py telegram_file_transport.py nginx-bilet-delivery.conf subscriptions.py subscription.json requirements.txt README.md make_brand.py LICENSE bilet-bot.service bilet-backup.service bilet-backup.timer config.py texts.py; do
    if [ -f "$bilet_root/$bilet_file" ]; then cp -a "$bilet_root/$bilet_file" "$bilet_backup/code/"; fi
done
for bilet_dir in biletnaya tests migrations brand scripts; do
    if [ -d "$bilet_root/$bilet_dir" ]; then cp -a "$bilet_root/$bilet_dir" "$bilet_backup/code/"; fi
done
if [ -f "$bilet_root/.env" ]; then cp -a "$bilet_root/.env" "$bilet_backup/environment"; fi
if [ "$bilet_enable_url" = 1 ]; then
    # The public route must have passed the separate gateway deployment first.
    test -f /etc/nginx/snippets/bilet-delivery.conf
    "$bilet_python" - <<'ROUTE'
from pathlib import Path
assert 'include /etc/nginx/snippets/bilet-delivery.conf;' in Path('/etc/nginx/sites-available/theatralka').read_text()
ROUTE
    nginx -t
fi
bilet_rollback() {
    trap - ERR
    systemctl stop bilet-bot.service || true
    for bilet_file in bot.py batches.py ticket_meta.py seat_tools.py eraser_tools.py validation.py progress.py delivery_cache.py url_delivery.py telegram_file_transport.py nginx-bilet-delivery.conf subscriptions.py subscription.json requirements.txt README.md make_brand.py LICENSE bilet-bot.service bilet-backup.service bilet-backup.timer config.py texts.py; do
        rm -f "$bilet_root/$bilet_file"
    done
    for bilet_dir in biletnaya tests migrations brand scripts; do rm -rf "$bilet_root/$bilet_dir"; done
    cp -a "$bilet_backup/code/." "$bilet_root/"
    if [ -f "$bilet_backup/environment" ]; then
        cp -a "$bilet_backup/environment" "$bilet_root/.env"
    fi
    # Never discard newly received payments or resume legacy per-file billing.
    if [ -f "$bilet_backup/code/subscriptions.py" ]; then
        systemctl start bilet-bot.service || true
    else
        printf 'Legacy backup restored; bot stopped to avoid per-file fees.\n' >&2
    fi
    printf 'ROLLBACK; backup=%s\n' "$bilet_backup" >&2
    exit 1
}
trap bilet_rollback ERR
systemctl stop bilet-bot.service
if [ -f "$bilet_root/state/subscriptions.sqlite3" ]; then
    "$bilet_python" - "$bilet_root/state/subscriptions.sqlite3" "$bilet_backup/subscriptions.sqlite3" <<'DB'
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as source, sqlite3.connect(sys.argv[2]) as target:
    source.backup(target)
DB
fi
cp "$bilet_stage/"{bot.py,batches.py,ticket_meta.py,seat_tools.py,eraser_tools.py,validation.py,progress.py,delivery_cache.py,url_delivery.py,telegram_file_transport.py,nginx-bilet-delivery.conf,subscriptions.py,requirements.txt,README.md,make_brand.py,LICENSE,config.py,texts.py} "$bilet_root/"
if [ "$bilet_enable_url" = 1 ]; then
    "$bilet_python" - "$bilet_root/.env" <<'ENVIRONMENT'
from pathlib import Path
import os, re, sys
path = Path(sys.argv[1])
text = path.read_text()
text = re.sub(r'(?m)^BILET_PUBLIC_DELIVERY=.*(?:\n|$)', '', text)
if text and not text.endswith('\n'):
    text += '\n'
text += 'BILET_PUBLIC_DELIVERY=https://theatralka.ru/-bilet-download\n'
temporary = path.with_name('.env.deploy')
temporary.write_text(text)
temporary.chmod(0o600)
os.replace(temporary, path)
ENVIRONMENT
fi
if [ ! -f "$bilet_root/subscription.json" ] || [ "$bilet_settings" = 1 ]; then
    cp "$bilet_stage/subscription.json" "$bilet_root/"
fi
for bilet_dir in biletnaya tests migrations brand scripts; do
    if [ ! -d "$bilet_stage/$bilet_dir" ]; then continue; fi
    rm -rf "$bilet_root/$bilet_dir"
    cp -a "$bilet_stage/$bilet_dir" "$bilet_root/$bilet_dir"
done
# Ежедневный бэкап state: скрипт + systemd-юниты (идемпотентно).
if [ -f "$bilet_stage/scripts/backup-state.sh" ]; then
    chmod 700 "$bilet_root/scripts/backup-state.sh"
    cp "$bilet_stage/bilet-backup.service" "$bilet_stage/bilet-backup.timer" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now bilet-backup.timer
    printf 'backup timer: %s\n' "$(systemctl is-enabled bilet-backup.timer)"
fi
cd "$bilet_root"
BILETNAYA="$bilet_root/biletnaya" "$bilet_python" - <<'VERIFY'
import bot
bot.get_store()
assert bot.CONFIG['price_stars'] == 1000
assert 1464797266 in bot.CONFIG['admin_ids']
assert 7868642254 in bot.CONFIG['admin_ids']
assert 'freedemin' in bot.CONFIG['gift_usernames']
print('Subscription configuration and database ready')
VERIFY
systemctl start bilet-bot.service
sleep 3
systemctl is-active --quiet bilet-bot.service
systemctl is-enabled --quiet bilet-bot.service
if [ "$bilet_enable_url" = 1 ]; then
    "$bilet_python" - <<'HEALTH'
import requests
response = requests.get('https://theatralka.ru/-bilet-download/deployment-health/probe.pdf', timeout=(5, 15))
assert response.status_code == 404, response.status_code
assert 'no-store' in response.headers.get('Cache-Control', '')
print('Running private delivery listener verified through public HTTPS')
HEALTH
fi
trap - ERR
rm -rf "$bilet_stage"
printf 'DEPLOYED; backup=%s\n' "$bilet_backup"
REMOTE
