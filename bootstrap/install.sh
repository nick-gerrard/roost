#!/usr/bin/env bash
# One-time privileged setup, run ON the server after `bootstrap/push.sh --seed`:
#   ssh -t linode 'sudo bash ~/roost/bootstrap/install.sh'
# Idempotent: safe to re-run. Does not touch any existing app.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }

U=nickg
HOME_U=/home/$U
SRC=$HOME_U/roost/bootstrap
FQDN=roost.nickgerrard.dev

[[ -f $HOME_U/.roost/roostd.env ]] || { echo "run bootstrap/push.sh --seed from your laptop first"; exit 1; }

echo "==> DNS check for $FQDN"
MYIP=$(curl -fsS4 https://api.ipify.org || hostname -I | awk '{print $1}')
GOT=$(getent ahostsv4 $FQDN | awk 'NR==1{print $1}' || true)
[[ "$GOT" == "$MYIP" ]] || { echo "$FQDN resolves to '${GOT:-nothing}', expected $MYIP. Add the Porkbun record first."; exit 1; }

echo "==> 2G swapfile (server builds on a 1 GB box)"
if [[ ! -e /swapfile ]]; then
    fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap -q /swapfile && swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
swapon --show

echo "==> /etc/roost + adoption markers for the two existing apps"
install -d -m 0755 /etc/roost /etc/roost/adopt
install -m 0644 $SRC/roost.conf /etc/roost/roost.conf
# One-shot permission for roost to take over the existing hand-written units/sites.
# Cleared automatically after each app's first successful roost deploy.
for app in milliways pwhl; do
    head -n1 /etc/systemd/system/$app-api.service 2>/dev/null | grep -qx '# roost-managed' \
        || touch /etc/roost/adopt/$app
done

echo "==> roost-apply + sudoers"
install -m 0755 -o root -g root $SRC/roost-apply /usr/local/sbin/roost-apply
install -m 0440 -o root -g root $SRC/sudoers.roostd /etc/sudoers.d/roostd
visudo -cf /etc/sudoers.d/roostd

echo "==> roostd service"
install -m 0644 $SRC/roostd.service /etc/systemd/system/roostd.service
systemctl daemon-reload
systemctl enable -q roostd
systemctl restart roostd
sleep 2
curl -fsS http://127.0.0.1:8900/healthz >/dev/null && echo "   roostd up on 127.0.0.1:8900"

echo "==> nginx + cert for $FQDN"
site() {
    cat <<NGINX
# managed by roost/bootstrap/install.sh
server {
    listen 80;
    server_name $FQDN;
$( [[ $1 == tls ]] && echo "    return 301 https://\$host\$request_uri;" || echo "    location / { proxy_pass http://127.0.0.1:8900; }" )
}
NGINX
    [[ $1 == tls ]] && cat <<NGINX
server {
    listen 443 ssl http2;
    server_name $FQDN;
    ssl_certificate /etc/letsencrypt/live/$FQDN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$FQDN/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
    client_max_body_size 5m;
    location / {
        proxy_pass http://127.0.0.1:8900;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
NGINX
    return 0
}
if [[ ! -e /etc/letsencrypt/live/$FQDN/fullchain.pem ]]; then
    site http > /etc/nginx/sites-available/roost
    ln -sfn /etc/nginx/sites-available/roost /etc/nginx/sites-enabled/roost
    nginx -t && systemctl reload nginx
    certbot certonly --nginx -n --cert-name $FQDN -d $FQDN
fi
site tls > /etc/nginx/sites-available/roost
ln -sfn /etc/nginx/sites-available/roost /etc/nginx/sites-enabled/roost
nginx -t && systemctl reload nginx

curl -fsS https://$FQDN/healthz >/dev/null && echo "==> done: https://$FQDN is live"
