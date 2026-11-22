#!/usr/bin/env bash
# Opens ports 80 and 443 in the server's own firewall (iptables). Run ON the server:
#
#     sudo bash scripts/setup-firewall.sh
#
# Oracle's Ubuntu images end the INPUT chain with a REJECT rule, so the new rules must go in
# BEFORE it. This is only half of the job: the cloud network's security list must also allow
# these ports (see docs/RUNBOOK.md). Safe to run again.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

for port in 80 443; do
  if iptables -C INPUT -p tcp -m state --state NEW --dport "$port" -j ACCEPT 2>/dev/null; then
    echo "port $port: already open"
  else
    position="$(iptables -L INPUT --line-numbers -n | awk '/REJECT/ {print $1; exit}')"
    if [ -n "$position" ]; then iptables -I INPUT "$position" -p tcp -m state --state NEW --dport "$port" -j ACCEPT
    else iptables -A INPUT -p tcp -m state --state NEW --dport "$port" -j ACCEPT; fi
    echo "port $port: opened"
  fi
done

# Keep the rules across reboots.
if command -v netfilter-persistent >/dev/null; then
  netfilter-persistent save
else
  mkdir -p /etc/iptables && iptables-save > /etc/iptables/rules.v4
  echo "saved to /etc/iptables/rules.v4 (install iptables-persistent to load it at boot)"
fi
iptables -L INPUT -n --line-numbers | head -12
