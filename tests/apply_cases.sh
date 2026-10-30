#!/usr/bin/env bash
# Runs inside a Linux container (or natively with NATIVE=1) — drives bootstrap/roost-apply
# against a fake root. Prints "RESULT <case> PASS|FAIL". Env: SRC W FAKE TEST_HOME TEST_APPS RUSER
set -u
: "${SRC:?}" "${W:?}" "${FAKE:?}" "${TEST_HOME:?}" "${TEST_APPS:?}" "${RUSER:?}"
LOG="$W/calls.log"; OUT="$W/out.txt"; STUBS="$W/stubs"
FQDN=milliways.nickgerrard.dev
mkdir -p "$STUBS"

stub() { printf '#!/bin/bash\n%s\n' "$2" > "$STUBS/$1"; chmod +x "$STUBS/$1"; }
stub systemctl 'echo "systemctl $*" >> '"$LOG"'; exit 0'
stub journalctl 'echo "journalctl $*" >> '"$LOG"'; exit 0'
stub openssl 'echo "openssl $*" >> '"$LOG"'; exit 0'
stub nginx 'echo "nginx $*" >> '"$LOG"'; [[ "$1" == -t && -e '"$W"'/nginx_fail ]] && exit 1; exit 0'
stub certbot 'echo "certbot $*" >> '"$LOG"'
while [[ $# -gt 0 ]]; do [[ "$1" == --cert-name ]] && n="$2"; shift; done
mkdir -p "'"$FAKE"'/etc/letsencrypt/live/$n"; : > "'"$FAKE"'/etc/letsencrypt/live/$n/fullchain.pem"'
# minimal rsync: rsync -rt --delete --no-links SRC/ DST/
stub rsync 'echo "rsync $*" >> '"$LOG"'
src="${@: -2:1}"; dst="${@: -1}"; find "$dst" -mindepth 1 -delete; cp -R "$src". "$dst"'
[[ "${NATIVE:-}" == 1 ]] && stub chown 'exit 0'
export PATH="$STUBS:$PATH"

reset() {
    rm -rf "${FAKE:?}" "${TEST_HOME:?}"; : > "$LOG"; rm -f "$W/nginx_fail"
    mkdir -p "$FAKE"/etc/{roost/adopt,systemd/system,nginx/sites-available,nginx/sites-enabled,letsencrypt/live} \
        "$FAKE/var/www" "$TEST_HOME"
    printf 'ROOST_USER=%s\nROOST_HOME=%s\nAPPS_ROOT=%s\nDOMAIN=nickgerrard.dev\n' \
        "$RUSER" "$TEST_HOME" "$TEST_APPS" > "$FAKE/etc/roost/roost.conf"
    cp -R "$W/staged" "$TEST_HOME/staged"
    printf 'server {\n    listen 80 default_server;\n    server_name _;\n}\n' > "$FAKE/etc/nginx/sites-available/default"
    ln -s "$FAKE/etc/nginx/sites-available/default" "$FAKE/etc/nginx/sites-enabled/default"
}
apply() { ROOST_TEST_ROOT="$FAKE" bash "$SRC/bootstrap/roost-apply" "$@" >"$OUT" 2>&1; RC=$?; }
S() { echo "$TEST_HOME/staged/milliways/$1"; }          # staged file path
mut() { perl -pi -e "$2" "$(S "$1")"; }                  # edit a staged file with a perl one-liner
need() { local d="$1"; shift; "$@" || { echo "  assertion failed: $d" >&2; exit 1; }; }
fails() { [[ $RC -ne 0 ]]; }
said() { grep -qF -- "$1" "$OUT"; }
logged() { grep -qF -- "$1" "$LOG"; }
nothing_installed() {
    [[ -z "$(ls "$FAKE/etc/systemd/system")" && ! -e "$FAKE/etc/nginx/sites-available/milliways" ]]
}

# rejection case: mutate, apply, expect failure with message and no side effects
rejects() { # file perl-expr message
    reset; mut "$1" "$2"; apply milliways milliways
    need "exit non-zero (rc=$RC)" fails
    need "message contains '$3' (got: $(head -c 300 "$OUT"))" said "$3"
    need "nothing installed" nothing_installed
    need "no systemctl calls" bash -c "! grep -q '^systemctl' '$LOG'"
}

case_happy_milliways() {
    reset; touch "$FAKE/etc/roost/adopt/milliways"
    apply milliways milliways
    need "rc 0 ($(cat "$OUT"))" test $RC -eq 0
    for u in api frontend; do
        need "unit $u installed" test -f "$FAKE/etc/systemd/system/milliways-$u.service"
        need "unit $u enabled" logged "systemctl enable -q milliways-$u"
        need "unit $u restarted" logged "systemctl restart milliways-$u"
    done
    need "daemon-reload" logged "systemctl daemon-reload"
    need "site installed" grep -q '^# roost-managed' "$FAKE/etc/nginx/sites-available/milliways"
    need "final site is TLS conf" grep -q 'listen 443 ssl' "$FAKE/etc/nginx/sites-available/milliways"
    need "enabled symlink" test "$(readlink "$FAKE/etc/nginx/sites-enabled/milliways")" = "$FAKE/etc/nginx/sites-available/milliways"
    need "certbot issued" logged "certbot certonly --nginx -n --cert-name $FQDN -d $FQDN"
    need "cert exists" test -e "$FAKE/etc/letsencrypt/live/$FQDN/fullchain.pem"
    need "nginx reloaded" logged "systemctl reload nginx"
    need "adopt marker removed" test ! -e "$FAKE/etc/roost/adopt/milliways"
    need "unit mode 0644" test "$(stat -c %a "$FAKE/etc/systemd/system/milliways-api.service" 2>/dev/null || stat -f %Lp "$FAKE/etc/systemd/system/milliways-api.service")" = 644
}

case_happy_pwhl_static() {
    reset; apply pwhl pwhl
    need "rc 0 ($(cat "$OUT"))" test $RC -eq 0
    need "www synced" test "$(cat "$FAKE/var/www/pwhl/index.html")" = hi
    need "api unit" test -f "$FAKE/etc/systemd/system/pwhl-api.service"
    need "no frontend unit" test ! -e "$FAKE/etc/systemd/system/pwhl-frontend.service"
    need "cert issued" logged "--cert-name pwhl.nickgerrard.dev"
}

case_rerun_existing_cert_no_certbot() {
    reset; apply milliways milliways; need "first run ok" test $RC -eq 0
    : > "$LOG"; cp -R "$W/staged/milliways" "$TEST_HOME/staged/" 2>/dev/null
    apply milliways milliways
    need "rc 0 ($(cat "$OUT"))" test $RC -eq 0
    need "no certbot (cert fresh)" bash -c "! grep -q '^certbot' '$LOG'"
    need "openssl checked expiry" logged "openssl x509 -checkend"
    need "backup of old unit made" test -n "$(ls "$FAKE/var/backups/roost/milliways" 2>/dev/null)"
}

case_refuses_foreign_unit() {
    reset; echo '[Service]' > "$FAKE/etc/systemd/system/milliways-api.service"
    apply milliways milliways
    need "fails" fails; need "says not roost-managed" said "not roost-managed"
    need "foreign unit untouched" test "$(cat "$FAKE/etc/systemd/system/milliways-api.service")" = '[Service]'
    need "nginx not installed" test ! -e "$FAKE/etc/nginx/sites-available/milliways"
}

case_refuses_foreign_nginx_site() {
    reset; echo 'server {}' > "$FAKE/etc/nginx/sites-available/milliways"
    apply milliways milliways
    need "fails" fails; need "says not roost-managed" said "not roost-managed"
    nothing_unit() { [[ -z "$(ls "$FAKE/etc/systemd/system")" ]]; }
    need "no units installed" nothing_unit
}

case_adopts_with_marker() {
    reset; echo '[Service]' > "$FAKE/etc/systemd/system/milliways-api.service"
    echo 'server {}' > "$FAKE/etc/nginx/sites-available/milliways"
    touch "$FAKE/etc/roost/adopt/milliways"
    apply milliways milliways
    need "rc 0 ($(cat "$OUT"))" test $RC -eq 0
    need "unit replaced" grep -q '^# roost-managed' "$FAKE/etc/systemd/system/milliways-api.service"
    need "old unit backed up" grep -rq '^\[Service\]$' "$FAKE/var/backups/roost/milliways"
    need "marker consumed" test ! -e "$FAKE/etc/roost/adopt/milliways"
}

case_failed_run_keeps_marker() {
    reset; touch "$FAKE/etc/roost/adopt/milliways"; mut milliways-api.service 's/^User=.*/User=root/'
    apply milliways milliways
    need "fails" fails; need "marker kept" test -e "$FAKE/etc/roost/adopt/milliways"
}

case_rejects_user_root()        { rejects milliways-api.service 's/^User=.*/User=root/' "User must be"; }
case_rejects_no_user_override() { rejects milliways-api.service '$_ .= "User=root\n" if /^User=/' "User must be"; }
case_rejects_execstartpre_plus(){ rejects milliways-api.service '$_ .= "ExecStartPre=+/bin/sh -c id\n" if /^User=/' "ExecStartPre"; }
case_rejects_execstart_plus()   { rejects milliways-api.service 's/^ExecStart=/ExecStart=+/' "ExecStart must be"; }
case_rejects_execstart_bin_sh() { rejects milliways-api.service 's/^ExecStart=.*/ExecStart=\/bin\/sh -c id/' "ExecStart must be"; }
case_rejects_execstart_dotdot() { rejects milliways-api.service 's/^ExecStart=.*/ExecStart=$ENV{TEST_APPS}\/..\/bin\/sh/' "ExecStart must be"; }
case_rejects_workdir_outside()  { rejects milliways-api.service 's/^WorkingDirectory=.*/WorkingDirectory=\/etc/' "bad WorkingDirectory"; }
case_rejects_envfile_outside()  { rejects milliways-api.service 's/^EnvironmentFile=.*/EnvironmentFile=-\/etc\/shadow/' "bad EnvironmentFile"; }
case_rejects_unit_no_marker()   { rejects milliways-api.service 's/^# roost-managed$/# hello/' "missing roost-managed marker"; }
case_rejects_leading_space_directive() { rejects milliways-api.service '$_ .= " ExecStartPost=/bin/id\n" if /^User=/' "not allowed"; }
case_rejects_unexpected_unit_name() {
    reset; cp "$(S milliways-api.service)" "$(S evil.service)"; apply milliways milliways
    need "fails" fails; need "says unexpected" said "unexpected unit"
    need "nothing installed" nothing_installed
}

case_rejects_foreign_server_name() { rejects milliways.nginx.conf 's/^(\s*server_name\s+)milliways\.nickgerrard\.dev;/$1evil.example.com;/' "server_name must be"; }
case_rejects_second_server_name()  { rejects milliways.nginx.conf 's/^(\s*server_name\s+milliways\.nickgerrard\.dev);/$1 other.example.com;/' "server_name must be"; }
case_rejects_include_passwd()      { rejects milliways.nginx.conf '$_ .= "    include /etc/passwd;\n" if /listen 443/' "include not allowed"; }
case_rejects_alias()               { rejects milliways.nginx.conf '$_ .= "    alias /etc/;\n" if /listen 443/' "alias"; }
case_rejects_proxy_foreign_host()  { rejects milliways.nginx.conf 's/proxy_pass http:\/\/127\.0\.0\.1:3002;/proxy_pass http:\/\/evil.example.com:80;/' "proxy_pass must target 127.0.0.1"; }
case_rejects_proxy_lookalike()     { rejects milliways.nginx.conf 's/proxy_pass http:\/\/127\.0\.0\.1:3002;/proxy_pass http:\/\/127.0.0.1.evil.com:80;/' "proxy_pass must target 127.0.0.1"; }
case_rejects_proxy_other_ip()      { rejects milliways.nginx.conf 's/proxy_pass http:\/\/127\.0\.0\.1:3002;/proxy_pass http:\/\/10.0.0.5:3002;/' "proxy_pass must target 127.0.0.1"; }
case_rejects_foreign_cert()        { rejects milliways.nginx.conf 's/live\/milliways\.nickgerrard\.dev\/privkey/live\/other.dev\/privkey/' "certificate must live"; }
case_rejects_root_elsewhere()      { rejects milliways.nginx.conf '$_ .= "    root /etc;\n" if /listen 443/' "root must be"; }
case_rejects_http_variant_too()    { rejects milliways.nginx.http.conf 's/server_name .*;/server_name evil.example.com;/' "server_name must be"; }
case_rejects_same_line_include()   { rejects milliways.nginx.conf 's/^(\s*listen 443 ssl http2;)/$1 include \/etc\/passwd;/' "directive roost does not generate"; }
case_rejects_access_log()          { rejects milliways.nginx.conf '$_ .= "    access_log /etc/cron.d/x;\n" if /listen 443/' "directive roost does not generate"; }
case_rejects_missing_nginx_marker(){ rejects milliways.nginx.conf 's/^# roost-managed$/# x/' "missing roost-managed marker"; }

case_rejects_other_site_same_fqdn() {
    reset
    printf 'server {\n    server_name foo.nickgerrard.dev %s;\n}\n' "$FQDN" > "$FAKE/etc/nginx/sites-available/other"
    ln -s "$FAKE/etc/nginx/sites-available/other" "$FAKE/etc/nginx/sites-enabled/other"
    apply milliways milliways
    need "fails" fails; need "says already served" said "already served by other"
    need "nothing installed" nothing_installed
}

case_other_site_different_fqdn_ok() {
    reset
    printf 'server {\n    server_name milliways.nickgerrard.dev.evil.com notmilliways.nickgerrard.dev;\n}\n' > "$FAKE/etc/nginx/sites-available/other"
    ln -s "$FAKE/etc/nginx/sites-available/other" "$FAKE/etc/nginx/sites-enabled/other"
    apply milliways milliways
    need "rc 0 ($(cat "$OUT"))" test $RC -eq 0
}

case_nginx_test_failure_rolls_back() {
    reset; touch "$W/nginx_fail"; apply milliways milliways
    need "fails" fails; need "says rejected" said "nginx config rejected"
    need "site removed" test ! -e "$FAKE/etc/nginx/sites-available/milliways" -a ! -L "$FAKE/etc/nginx/sites-enabled/milliways"
}

case_bad_app_name() {
    reset; apply 'Bad_Name' x; need "rc 2" test $RC -eq 2; need "msg" said "bad app name"
    reset; apply '../etc' x; need "rc 2 (traversal)" test $RC -eq 2
    reset; apply milliways 'Bad.Sub'; need "rc 2 sub" test $RC -eq 2; need "msg" said "bad subdomain"
    reset; apply milliways roost; need "rc 2 reserved" test $RC -eq 2; need "msg" said "reserved"
    reset; apply nostaged x; need "rc 2 no staged" test $RC -eq 2; need "msg" said "no staged dir"
}

case_no_subdomain_worker() {
    reset; rm -f "$(S milliways.nginx.conf)" "$(S milliways.nginx.http.conf)"
    apply milliways -
    need "rc 0 ($(cat "$OUT"))" test $RC -eq 0
    need "unit installed" test -f "$FAKE/etc/systemd/system/milliways-api.service"
    need "no site" test ! -e "$FAKE/etc/nginx/sites-available/milliways"
    need "no certbot" bash -c "! grep -q '^certbot' '$LOG'"
}

case_missing_nginx_files_with_subdomain() {
    reset; rm -f "$(S milliways.nginx.http.conf)"; apply milliways milliways
    need "fails" fails; need "msg" said "missing staged nginx config"
}

for fn in $(declare -F | awk '{print $3}' | grep '^case_'); do
    if ( "$fn" ) 2>"$W/err.txt"; then echo "RESULT ${fn#case_} PASS"
    else echo "RESULT ${fn#case_} FAIL"; sed 's/^/    /' "$W/err.txt"; sed 's/^/    out: /' "$OUT"; fi
done
