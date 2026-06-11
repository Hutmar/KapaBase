# acl.py
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
    """
    Prüft, ob der Client des aktuellen Requests das Recht für eine Aktion in einem Scope hat.
    Struktur in acl.json: { "scope": { "action": ["host1", "host2"] } }
    """
    client_host = request.client.host if request.client else None
    if not client_host:
        return False

    # ACL laden
    acl = _load_acl()
    
    # Hole die erlaubten Hosts für die spezifische Aktion im Scope
    scope_config = acl.get(scope, {})
    if not isinstance(scope_config, dict):
        return False
        
    allowed_hosts = scope_config.get(action, [])

    # Wenn die IP direkt in der Liste steht
    if client_host in allowed_hosts:
        return True

    # Reverse DNS Lookup, um Hostnamen / FQDN des Clients zu bestimmen
    try:
        hostname, aliases, _ = socket.gethostbyaddr(client_host)
        possible_names = [hostname] + aliases
        
        for name in possible_names:
            if name in allowed_hosts:
                return True
                
    except socket.herror:
        # Fallback wenn kein Reverse-DNS-Eintrag existiert
        pass

    return False