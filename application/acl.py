import json
import os
import socket
from fastapi import Request

ACL_FILE = "acl.json"

def _load_acl() -> dict:
    if not os.path.exists(ACL_FILE):
        return {}
    try:
        with open(ACL_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def has_permission(request: Request, scope: str, action: str = "edit") -> bool:
    client_host = request.client.host if request.client else None
    if not client_host:
        return False

    acl = _load_acl()
    scope_config = acl.get(scope, {})
    if not isinstance(scope_config, dict):
        return False
        
    allowed_hosts = scope_config.get(action, [])

    # 1. Direkter IP-Abgleich (Schnellster Weg)
    if client_host in allowed_hosts:
        return True

    # 2. Forward DNS Lookup für die Einträge aus der ACL
    for host in allowed_hosts:
        # Überspringe Einträge, die ohnehin schon wie IPs aussehen
        if host.replace(".", "").isdigit(): 
            continue
            
        try:
            # Versuche den Hostnamen aus der ACL in IPs aufzulösen
            # getaddrinfo gibt alle IPs (IPv4/IPv6) zurück, die zu dem Namen gehören
            resolved_ips = {res[4][0] for res in socket.getaddrinfo(host, None)}
            
            if client_host in resolved_ips:
                return True
        except socket.gaierror:
            # Hostname konnte nicht aufgelöst werden (z.B. offline oder Tippfehler)
            continue

    return False