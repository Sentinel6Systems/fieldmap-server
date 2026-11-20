"""fieldmap-server: network isolation in NetBird, for remote access through NetBird.

Several Fieldmap servers can share one NetBird account. Each server and the devices paired
with it get a segment of their own, and nothing in one segment reaches another:

  * two groups for this server only: fieldmap-server-<id> (the server's own peer) and
    fieldmap-devices-<id> (the devices paired with it);
  * one access policy, fieldmap-<id>: from the device group to the server group, one direction,
    TCP, the gateway's port only. Devices never talk to each other (location sharing goes
    through the server), and the server never starts a connection to a device. NetBird gives a
    peer only the peers that a policy links it to, so a device sees its own server and nothing
    else, and the segments of other servers are not even in its network map;
  * a reusable setup key for this server's devices, whose auto-group is the device group. It is
    the key paired devices get (tools/remote-server.py netbird_block()). The group comes from
    the key, inside NetBird: nothing a device sends can put it into another segment.

The owner pastes a NetBird access token on the Remote Access page. It needs NetBird's Admin
role (only Admin may create setup keys), and an Admin token can change the whole account, every
other server's segment included. So it is used once, in memory, and never written anywhere:
setup(), check() and replace_key() take it as an argument and forget it when they return. What
they did is written down (ids, names, times, results), never the token.

Every run is safe to repeat: things are found by the ids recorded the last time, and a group,
policy or key with our name that this server did not record is refused, never taken over.
A run that fails anywhere stops there, keeps what was done, and says what failed; the page then
never says that isolation is verified (status()).

Python's standard library only. tools/test_remote_server.py tests it against a fake NetBird API.
"""
import ipaddress
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

KEY_DAYS = 365          # the device key's life; a device that joined keeps working after it expires
KEY_USES = 100          # how many devices may join with one device key
KEY_WARN_DAYS = 30      # the page warns this long before the device key expires
CHECK_DAYS = 30         # a check older than this is not recent any more
TIMEOUT = 20            # seconds for one call to the NetBird API
ADMIN_ROLES = ("owner", "admin")


class IsolationError(Exception):
    """A step that failed, in words for the owner. Never holds the token or a setup key."""


# ---------- the NetBird API ----------

def api_base(management):
    """The NetBird API of a management server: the same address, path /api (NetBird's cloud and
    a self-hosted server both serve it there)."""
    parts = urllib.parse.urlsplit(management)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise IsolationError("Invalid management URL.")
    return "%s://%s/api" % (parts.scheme, parts.netloc)


def clean_token(text):
    """A NetBird access token as pasted ("nbp_" and letters and digits); "" if it is not one."""
    text = str(text or "").strip()
    return text if re.fullmatch(r"[A-Za-z0-9_]{20,128}", text) else ""


def http_transport(method, url, headers, body):
    """Sends one request; returns (status, decoded JSON or None)."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
            data = answer.read()
            return answer.status, json.loads(data) if data else None
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None


class Api:
    """The few calls of NetBird's management API this needs. The token stays in this object."""

    def __init__(self, management, token, transport=None):
        self.base = api_base(management)
        self._token = token
        self._transport = transport or http_transport

    def call(self, method, path, body=None):
        headers = {"Authorization": "Token " + self._token, "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        try:
            status, answer = self._transport(method, self.base + path, headers, data)
        except (OSError, ValueError) as e:
            raise IsolationError("Cannot reach the NetBird API at %s (%s)." % (self.base, type(e).__name__))
        if status == 401:
            raise IsolationError("NetBird did not accept the access token. It may be mistyped, expired, or deleted.")
        if status == 403:
            raise IsolationError("The access token is not allowed to do this. It needs the Admin role.")
        if not 200 <= status < 300:
            said = answer.get("message", "") if isinstance(answer, dict) else ""
            raise IsolationError("NetBird API error %d on %s %s%s" % (
                status, method, path.split("?")[0], (": " + str(said)[:200]) if said else "."))
        return answer

    def get(self, path):
        return self.call("GET", path)

    def post(self, path, body):
        return self.call("POST", path, body)

    def put(self, path, body):
        return self.call("PUT", path, body)


# ---------- names, ids, and what the policy must be ----------

def new_id():
    return secrets.token_hex(5)


def names(install_id):
    return {"server": "fieldmap-server-" + install_id, "devices": "fieldmap-devices-" + install_id,
            "policy": "fieldmap-" + install_id, "key": "fieldmap-devices-" + install_id}


def ids(items):
    """The ids in a list of NetBird references: plain ids, or objects with "id"."""
    out = []
    for item in items or []:
        out.append(item.get("id") if isinstance(item, dict) else item)
    return [i for i in out if i]


def wanted_rule(record, port):
    return {"name": names(record["id"])["policy"], "description": "Fieldmap devices to their server: gateway port only",
            "enabled": True, "action": "accept", "bidirectional": False, "protocol": "tcp", "ports": [str(port)],
            "sources": [record["deviceGroup"]], "destinations": [record["serverGroup"]]}


def wanted_policy(record, port):
    return {"name": names(record["id"])["policy"], "description": "Fieldmap: devices of one server reach that server's gateway only",
            "enabled": True, "rules": [wanted_rule(record, port)]}


def policy_matches(policy, record, port):
    """Our policy is exactly what it must be: nothing wider, nothing missing."""
    if not policy.get("enabled") or len(policy.get("rules") or []) != 1:
        return False
    rule = policy["rules"][0]
    return (rule.get("enabled") is True and rule.get("action") == "accept" and rule.get("bidirectional") is False
            and rule.get("protocol") == "tcp" and [str(p) for p in rule.get("ports") or []] == [str(port)]
            and not rule.get("port_ranges") and not rule.get("source") and not rule.get("destination")
            and ids(rule.get("sources")) == [record["deviceGroup"]]
            and ids(rule.get("destinations")) == [record["serverGroup"]])


def find(items, key, value):
    return [i for i in items or [] if isinstance(i, dict) and i.get(key) == value]


# ---------- the run ----------

def check_role(api, need_admin):
    me = api.get("/users/current") or {}
    role = str(me.get("role", ""))
    if need_admin and role not in ADMIN_ROLES:
        raise IsolationError("The access token belongs to a user with the role \"%s\". Setup needs a token of a user "
                             "with the Admin role." % (role or "unknown"))
    return role


def own_group(api, groups, record, field, name):
    """Our group: the one with the recorded id, made if there is none. A group with our name that we
    did not record is someone else's: refused."""
    gid = record.get(field)
    if gid:
        mine = find(groups, "id", gid)
        if mine:
            if mine[0].get("name") != name:
                raise IsolationError("The group %s was renamed in NetBird to \"%s\". Rename it back, then try again." % (name, mine[0].get("name")))
            return mine[0]
    if find(groups, "name", name):
        raise IsolationError("A group named %s exists in NetBird but was not created by this server. Delete or rename it "
                             "in the NetBird dashboard, then try again." % name)
    made = api.post("/groups", {"name": name, "peers": []})
    record[field] = made["id"]
    return made


def add_peers(api, group, peer_ids):
    """Adds peers to one of our groups. NetBird's group update replaces the whole member list:
    the list is read, added to, and written back. Nothing is ever taken out."""
    have = ids(group.get("peers"))
    new = [p for p in peer_ids if p not in have]
    if new:
        api.put("/groups/" + group["id"], {"name": group["name"], "peers": have + new})
    return new


def server_peer(peers, address):
    """The server's own peer, found by its NetBird address (unique in an account)."""
    if not address:
        raise IsolationError("The server is not connected to NetBird yet. Connect NetBird with a setup key first.")
    mine = find(peers, "ip", address)
    if len(mine) != 1:
        raise IsolationError("The server's NetBird address %s does not match one peer in this NetBird account. "
                             "Check that the token is for the account the server is connected to." % address)
    return mine[0]


def setup(record, management, token, address, port, devices, keep_key, transport=None, new_key=False, now=None):
    """Creates or repairs this server's groups, policy and device key, moves its known devices into
    the device group, and checks isolation. `record` is what the last run wrote down (a dict,
    changed in place); `address` the server's NetBird address; `port` the gateway's port;
    `devices` {device id: NetBird address it came from}; `keep_key(key)` stores a new device key
    on the server (a secret) at once, before anything else can fail. Raises IsolationError when a
    step fails; `record` then holds what was done until then and the error."""
    now = int(now or time.time())
    api = Api(management, token, transport)
    record.update(lastRun=now, lastError="", step="token")
    try:
        check_role(api, need_admin=True)
        record.setdefault("id", new_id())
        record["management"] = management
        n = names(record["id"])
        record["step"] = "server peer"
        peers = api.get("/peers") or []
        me = server_peer(peers, address)
        record["serverPeer"] = me["id"]
        record["serverAddress"] = address
        record["step"] = "groups"
        groups = api.get("/groups") or []
        server_group = own_group(api, groups, record, "serverGroup", n["server"])
        device_group = own_group(api, groups, record, "deviceGroup", n["devices"])
        add_peers(api, server_group, [me["id"]])
        record["step"] = "policy"
        policies = api.get("/policies") or []
        pid = record.get("policy")
        mine = find(policies, "id", pid) if pid else []
        if not mine and find(policies, "name", n["policy"]):
            raise IsolationError("A policy named %s exists in NetBird but was not created by this server. Delete or rename "
                                 "it in the NetBird dashboard, then try again." % n["policy"])
        if not mine:
            record["policy"] = api.post("/policies", wanted_policy(record, port))["id"]
        elif not policy_matches(mine[0], record, port):
            api.put("/policies/" + pid, wanted_policy(record, port))
        record["policyPort"] = port
        record["step"] = "device key"
        if new_key or not record.get("deviceKey"):
            make_key(api, record, keep_key, now)
        else:
            revoke_old(api, record)
        record["step"] = "devices"
        migrate(api, record, peers, device_group, devices)
        record["step"] = "check"
        _check(api, record, port, devices, now)
        record["step"] = ""
    except IsolationError as e:
        record["lastError"] = "%s (step: %s)" % (e, record.get("step"))
        record["checkOk"] = False
        raise


def make_key(api, record, keep_key, now):
    """A new device key: reusable, the device group as its auto-group, a use limit and a life.
    The key itself exists only in NetBird's answer (NetBird keeps a hash): keep_key stores it.
    The key it replaces is revoked afterwards, only by its recorded id: devices that joined with
    it stay members (NetBird does not look at a key again once a device has joined)."""
    n = names(record["id"])
    keys = api.get("/setup-keys") or []
    old = record.get("deviceKey", {}).get("id")
    others = [k for k in find(keys, "name", n["key"]) if k.get("id") != old and not k.get("revoked")]
    if others and not old:
        raise IsolationError("A setup key named %s exists in NetBird but was not created by this server. Revoke it in "
                             "the NetBird dashboard, then try again." % n["key"])
    made = api.post("/setup-keys", {"name": n["key"], "type": "reusable", "expires_in": KEY_DAYS * 86400,
                                    "auto_groups": [record["deviceGroup"]], "usage_limit": KEY_USES,
                                    "ephemeral": False, "allow_extra_dns_labels": False})
    plain = str(made.get("key", ""))
    if not re.fullmatch(r"[A-Za-z0-9-]{16,128}", plain):
        raise IsolationError("NetBird did not return the new setup key.")
    keep_key(plain)
    record["deviceKey"] = {"id": made["id"], "made": now, "expires": now + KEY_DAYS * 86400}
    if old:
        record["unrevoked"] = old
    revoke_old(api, record)


def revoke_old(api, record):
    """Revokes the device key a new one replaced. If NetBird says no, the next run tries again."""
    old = record.get("unrevoked")
    if not old:
        return
    try:
        api.put("/setup-keys/" + old, {"auto_groups": [record["deviceGroup"]], "revoked": True})
        record["unrevoked"] = ""
    except IsolationError:
        pass


def migrate(api, record, peers, device_group, devices):
    """Puts the paired devices that came in through NetBird into the device group, found by the
    NetBird address their certificate came from (never by name: anyone can choose a name)."""
    by_ip = {p.get("ip"): p for p in peers if isinstance(p, dict)}
    found, unmatched = [], []
    for device_id, address in sorted(devices.items()):
        peer = by_ip.get(address) if address else None
        if peer and peer["id"] != record.get("serverPeer"):
            found.append(peer["id"])
        else:
            unmatched.append(device_id)
    added = add_peers(api, device_group, found)
    record["moved"] = len(added)
    record["unmatched"] = unmatched


# ---------- the check ----------

def check(record, management, token, port, devices, transport=None, now=None):
    """Checks isolation with a token of any role that can read (Auditor is enough). Changes nothing."""
    now = int(now or time.time())
    if not record.get("id"):
        raise IsolationError("Network isolation is not set up on this server.")
    api = Api(management, token, transport)
    record.update(lastError="", step="check")
    try:
        check_role(api, need_admin=False)
        _check(api, record, port, devices, now)
        record["step"] = ""
    except IsolationError as e:
        record["lastError"] = "%s (step: check)" % e
        record["checkOk"] = False
        raise


def _check(api, record, port, devices, now):
    """Reads the account and writes down whether this server's segment is closed to everything else.
    Problems break isolation; warnings do not but need the owner."""
    problems, warnings = [], []
    groups = api.get("/groups") or []
    peers = api.get("/peers") or []
    policies = api.get("/policies") or []
    n = names(record["id"])
    server_group = find(groups, "id", record.get("serverGroup"))
    device_group = find(groups, "id", record.get("deviceGroup"))
    all_group = [g["id"] for g in groups if g.get("name") == "All"]
    if not server_group or not device_group:
        problems.append("The group %s or %s is missing in NetBird. Run Set Up Network Isolation again." % (n["server"], n["devices"]))
    server_members = ids(server_group[0].get("peers")) if server_group else []
    device_members = ids(device_group[0].get("peers")) if device_group else []
    if record.get("serverPeer") not in server_members:
        problems.append("This server's peer is not in the group %s." % n["server"])
    strangers = [p for p in server_members if p != record.get("serverPeer")]
    if strangers:
        problems.append("The group %s holds %d other peer(s). Devices of this server can reach them. Remove them from "
                        "the group (a copied server installation does this)." % (n["server"], len(strangers)))
    # Who else could reach our peers: every enabled policy other than ours that names our groups,
    # the All group (it holds every peer), or one of our peers directly.
    ours = set(server_members) | set(device_members)
    watched = {record.get("serverGroup"), record.get("deviceGroup"), *all_group}
    own = find(policies, "id", record.get("policy"))
    if not own:
        problems.append("The access policy %s is missing in NetBird. Run Set Up Network Isolation again." % n["policy"])
    elif not policy_matches(own[0], record, port):
        problems.append("The access policy %s was changed or disabled in NetBird, or the gateway port changed. Run Set Up "
                        "Network Isolation again to repair it." % n["policy"])
    for policy in policies:
        if policy.get("id") == record.get("policy") or not policy.get("enabled"):
            continue
        for rule in policy.get("rules") or []:
            if not rule.get("enabled"):
                continue
            refs = set(ids(rule.get("sources"))) | set(ids(rule.get("destinations")))
            direct = {r.get("id") for r in (rule.get("source"), rule.get("destination"))
                      if isinstance(r, dict) and r.get("type") == "peer"}
            if refs & watched or direct & ours:
                name = policy.get("name") or policy.get("id")
                if name == "Default" and refs & set(all_group):
                    problems.append("The account-wide policy \"Default\" (All to All) lets every peer in the NetBird account "
                                    "reach every other, this server's devices included. See the User Guide for how to "
                                    "disable it safely.")
                else:
                    problems.append("The policy \"%s\" also reaches this server's peers." % name)
                break
    # Warnings: things the owner should look at that do not open the segment.
    paired_ips = {a for a in devices.values() if a}
    by_id = {p.get("id"): p for p in peers if isinstance(p, dict)}
    stale = [by_id[p].get("name", p) for p in device_members if p in by_id and by_id[p].get("ip") not in paired_ips]
    if stale:
        warnings.append("Peers in %s that are not paired with this server (removed devices?): %s. Delete them under "
                        "Peers in the NetBird dashboard." % (n["devices"], ", ".join(sorted(map(str, stale))[:10])))
    if record.get("unmatched"):
        warnings.append("%d paired device(s) have not connected through NetBird yet, so they could not be moved into %s. "
                        "Check again after they connected once from outside." % (len(record["unmatched"]), n["devices"]))
    try:
        accounts = api.get("/accounts") or []
        settings = (accounts[0] if accounts else {}).get("settings") or {}
    except IsolationError:
        settings = {}
        warnings.append("The NetBird account's settings could not be read, so peer approval and peer expose were not checked.")
    if (settings.get("extra") or {}).get("peer_approval_enabled"):
        warnings.append("Peer approval is enabled in this NetBird account: a newly paired device cannot connect until "
                        "it is approved in the NetBird dashboard.")
    if settings.get("peer_expose_enabled") and set(settings.get("peer_expose_groups") or []) & watched:
        warnings.append("Peer expose is enabled for a group that holds this server's peers: they can publish services "
                        "through NetBird.")
    if record.get("unrevoked"):
        warnings.append("The previous device setup key could not be revoked. Revoke it in the NetBird dashboard "
                        "(Setup Keys, name %s), or run Set Up Network Isolation again." % n["key"])
    key = record.get("deviceKey") or {}
    if key.get("expires") and key["expires"] - now < KEY_WARN_DAYS * 86400:
        warnings.append("The device setup key expires on %s. Devices that are paired already keep working; to pair new "
                        "ones after that, click Replace Device Key." % time.strftime("%Y-%m-%d", time.localtime(key["expires"])))
    record.update(lastCheck=now, checkOk=not problems, problems=problems, warnings=warnings,
                  deviceCount=len(device_members), checkPort=port, checkAddress=record.get("serverAddress"))


# ---------- for the page ----------

def status(record, port, address, now=None):
    """What the Remote Access page shows. Never says verified unless the last check passed, is
    recent, and still fits the gateway port and the server's NetBird address."""
    now = int(now or time.time())
    out = {"managed": bool(record.get("id")), "id": record.get("id", ""), "serverGroup": "", "deviceGroup": "",
           "deviceCount": record.get("deviceCount", 0), "unmatched": len(record.get("unmatched") or []),
           "policy": "", "keyExpires": (record.get("deviceKey") or {}).get("expires", 0),
           "lastCheck": record.get("lastCheck", 0), "problems": list(record.get("problems") or []),
           "warnings": list(record.get("warnings") or []), "error": record.get("lastError", "")}
    if not out["managed"]:
        return {**out, "status": "none", "text": "Not Managed. Devices use the setup key entered below, and no access "
                "policy limits what they can reach in the NetBird account."}
    n = names(record["id"])
    out.update(serverGroup=n["server"], deviceGroup=n["devices"], policy=n["policy"])
    if record.get("lastError"):
        return {**out, "status": "failed", "text": "Setup Failed: " + record["lastError"]}
    if not record.get("lastCheck"):
        return {**out, "status": "failed", "text": "Not checked yet. Click Check Isolation."}
    if not record.get("checkOk"):
        return {**out, "status": "open", "text": "Not Isolated. See the problems below."}
    if record.get("checkPort") != port or (address and record.get("checkAddress") != address):
        return {**out, "status": "stale", "text": "Not Recently Verified: the gateway port or the server's NetBird address "
                "changed since the last check. Run Set Up Network Isolation again."}
    if now - record["lastCheck"] > CHECK_DAYS * 86400:
        return {**out, "status": "stale", "text": "Not Recently Verified: the last check is more than %d days old. Click "
                "Check Isolation." % CHECK_DAYS}
    return {**out, "status": "verified", "text": "Verified by policy check on %s. This is a check of NetBird's access "
            "policies, not a traffic test." % time.strftime("%Y-%m-%d %H:%M", time.localtime(record["lastCheck"]))}


def valid_address(text):
    try:
        return str(ipaddress.ip_address(str(text or "").strip()))
    except ValueError:
        return ""
