"""Evidence-based LLDP/CDP port-to-port correlation.

This module is deliberately pure: it never promotes an unknown remote port to
a physical link, and a link is Confirmed only when the exact endpoint pair is
observed from both devices.
"""
import re
from datetime import datetime, timezone


def normalize_interface(value):
    """Normalize common Cisco aliases while retaining slot/port/subport data."""
    text = re.sub(r"\s+", "", str(value or "")).lower()
    prefixes = (
        (r"^(?:tengigabitethernet|tengige|te)(?=\d)", "te"),
        (r"^(?:gigabitethernet|gig|gi)(?=\d)", "gi"),
        (r"^(?:fastethernet|fa)(?=\d)", "fa"),
        (r"^(?:ethernet|eth|et|e)(?=\d)", "eth"),
        (r"^(?:port-channel|portchannel|po)(?=\d)", "po"),
        (r"^(?:fortygigabitethernet|fo)(?=\d)", "fo"),
        (r"^(?:hundredgigabitethernet|hu)(?=\d)", "hu"),
    )
    for pattern, replacement in prefixes:
        if re.match(pattern, text):
            return re.sub(pattern, replacement, text)
    return text


def is_physical_interface(value):
    return bool(normalize_interface(value)) and not re.match(
        r"^(?:po\d|lag\d|vlan\d|vl\d|loopback|lo\d|null|tunnel|nve)",
        normalize_interface(value),
    )


def _id_values(device):
    values = set()
    for field in ("name", "sys_name", "management_ip", "chassis_id", "serial_number"):
        raw = str(device.get(field) or "").strip().strip('"')
        if raw:
            values.add(raw.casefold())
    return values


def correlate(devices, interfaces, observations, manual_links=(), legacy_links=(), now=None, stale_seconds=86400):
    """Return corroborated point-to-point mappings and unresolved evidence.

    Required observation fields match ``network_neighbors``. Every endpoint is
    resolved against an observed interface. Manual links are retained as
    authoritative records and suppress only the exact discovered duplicate.
    """
    by_device = {int(d["id"]): d for d in devices}
    identities = {}
    for d in devices:
        for value in _id_values(d):
            identities.setdefault(value, set()).add(int(d["id"]))
    ports = {}
    port_rows = {}
    by_index = {}
    for p in interfaces:
        did = int(p["device_id"])
        name = str(p.get("if_name") or "").strip()
        if not name:
            continue
        ports[(did, normalize_interface(name))] = name
        port_rows[(did, normalize_interface(name))] = p
        if p.get("if_index") is not None:
            by_index[(did, int(p["if_index"]))] = name

    def remote_device(row):
        candidates=set()
        for field in ("remote_chassis_id", "remote_device", "remote_ip"):
            value=str(row.get(field) or "").strip().strip('"').casefold()
            forms=[value]
            # Cisco CDP's documented device-id format includes a serial suffix.
            # Require a unique exact inventory identity after removing only that suffix.
            match=re.fullmatch(r"(.+)\(([a-z0-9-]+)\)",value)
            if match: forms.append(match.group(1))
            for form in forms:
                ids=identities.get(form,set())
                if len(ids)==1:candidates.update(ids)
        return next(iter(candidates)) if len(candidates)==1 else None

    aliases={}
    for p in interfaces:
        name=ports.get((int(p['device_id']),normalize_interface(p.get('if_name'))))
        for field in ('if_name','if_descr','if_alias'):
            alias=normalize_interface(p.get(field))
            if alias and name:aliases.setdefault((int(p['device_id']),alias),set()).add(name)
    def resolve_port(did,label):
        options=aliases.get((did,normalize_interface(label)),set())
        return next(iter(options)) if len(options)==1 else None
    normalized = []
    unresolved = []
    for row in observations:
        did = int(row.get("device_id") or 0)
        local = by_index.get((did, int(row.get("local_if_index") or 0)))
        local = local or ports.get((did, normalize_interface(row.get("local_if_name"))))
        peer = remote_device(row)
        remote_label = str(row.get("remote_port") or row.get("remote_port_id") or "").strip()
        subtype = str(row.get("remote_port_subtype") or "").strip().casefold()
        subtype=re.sub(r".*\((\d+)\)$",r"\1",subtype)
        subtype_is_ifname = subtype in ("5", "if_name", "ifname", "interface_name", "interfacename")
        if str(row.get("protocol") or "").upper() == "CDP" and subtype in ("if_name", "ifname", "interface_name", "interfacename"):
            subtype_is_ifname = True
        remote = resolve_port(peer,remote_label) if peer else None
        local_key, remote_key = normalize_interface(local), normalize_interface(remote)
        supported_local = is_physical_interface(local) or local_key.startswith("po")
        supported_remote = is_physical_interface(remote) or remote_key.startswith("po")
        if not local or not peer or peer == did or not remote or not supported_local or not supported_remote or not subtype_is_ifname:
            unresolved.append({
                "device_id": did, "local_port": local or row.get("local_if_name") or "Unknown",
                "remote_device_id": peer, "remote_device": row.get("remote_device") or "Unknown",
                "remote_port": remote_label or "Unknown", "protocol": str(row.get("protocol") or "Unknown").upper(),
                "status": "UNKNOWN_UNVERIFIED", "last_seen": row.get("last_seen"),
                "reason": "Remote device/interface is unmatched or LLDP Port ID subtype is not verified as ifName",
            })
            continue
        left, right = (did, normalize_interface(local)), (peer, normalize_interface(remote))
        key = tuple(sorted((left, right)))
        normalized.append({**dict(row), "device_id": did, "local_port": local,
                           "remote_device_id": peer, "remote_port_name": remote,
                           "endpoint_key": key})

    # Count only exact reverse endpoint evidence; observations from both boxes
    # alone are insufficient to establish a reciprocal port map.
    directed = set((x["device_id"], normalize_interface(x["local_port"]),
                    x["remote_device_id"], normalize_interface(x["remote_port_name"])) for x in normalized)
    endpoint_claims = {}
    reverse_claims = {}
    for x in normalized:
        endpoint_claims.setdefault((x["device_id"], normalize_interface(x["local_port"])), set()).add(
            (x["remote_device_id"], normalize_interface(x["remote_port_name"]))
        )
        reverse_claims.setdefault((x["device_id"], normalize_interface(x["local_port"]), x["remote_device_id"]), set()).add(
            normalize_interface(x["remote_port_name"])
        )
    grouped = {}
    for x in normalized:
        key = x["endpoint_key"]
        reverse = (x["remote_device_id"], normalize_interface(x["remote_port_name"]),
                   x["device_id"], normalize_interface(x["local_port"])) in directed
        reverse_targets = reverse_claims.get((x["remote_device_id"], normalize_interface(x["remote_port_name"]), x["device_id"]), set())
        conflict = (len(endpoint_claims[(x["device_id"], normalize_interface(x["local_port"]))]) > 1 or
                    bool(reverse_targets and normalize_interface(x["local_port"]) not in reverse_targets))
        g = grouped.setdefault(key, {"evidence": [], "confirmed": False, "conflict": False})
        g["evidence"].append(x)
        g["confirmed"] = g["confirmed"] or reverse
        g["conflict"] = g["conflict"] or conflict

    # Any single interface claimed by two different endpoint pairs is a
    # conflict at both ends (including when the inconsistent claim comes only
    # from the peer's LLDP/CDP row).
    endpoint_groups = {}
    for key in grouped:
        for endpoint in key:
            endpoint_groups.setdefault(endpoint, set()).add(key)
    for keys in endpoint_groups.values():
        if len(keys) > 1:
            for key in keys:
                grouped[key]["conflict"] = True

    links = []
    for index, (key, group) in enumerate(sorted(grouped.items()), 1):
        (a, ap), (b, bp) = key
        # Names are taken from the interface inventory, never from a neighbor
        # string, so the returned endpoints are usable by the port panel.
        aport, bport = ports[(a, ap)], ports[(b, bp)]
        evidence = group["evidence"]
        protocols = sorted({str(x.get("protocol") or "Unknown").upper() for x in evidence})
        status = "CONFLICT" if group["conflict"] else "CONFIRMED" if group["confirmed"] else "ONE_SIDED"
        freshness="CURRENT"
        if now is not None:
            try:
                stamp=max(datetime.fromisoformat(str(x.get('last_seen')).replace('Z','+00:00')) for x in evidence if x.get('last_seen'))
                if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone.utc)
                if (now-stamp).total_seconds()>stale_seconds:freshness="STALE"
            except (ValueError,TypeError):freshness="UNKNOWN"
        links.append({
            "freshness":freshness,"verification_status":status,
            "id": f"discovered:{a}:{ap}:{b}:{bp}", "source_device_id": a, "source_port": aport,
            "target_device_id": b, "target_port": bport, "status": status,
            "source": "DISCOVERY", "protocols": protocols,
            "evidence_count": len(evidence),
            "last_seen": max((str(x.get("last_seen") or "") for x in evidence), default=""),
            "observations": [{"device_id": x["device_id"], "local_port": x["local_port"],
                              "remote_port": x["remote_port_name"], "protocol": str(x.get("protocol") or "").upper()}
                             for x in evidence],
            "link_type": "PORT_CHANNEL" if normalize_interface(aport).startswith("po") or normalize_interface(bport).startswith("po") else "PHYSICAL",
            "source_oper_status": port_rows.get((a, ap), {}).get("oper_status") or "Unknown",
            "target_oper_status": port_rows.get((b, bp), {}).get("oper_status") or "Unknown",
            "source_speed_bps": None if port_rows.get((a,ap),{}).get("speed_bps")==4294967295 else port_rows.get((a,ap),{}).get("speed_bps"),
            "target_speed_bps": None if port_rows.get((b,bp),{}).get("speed_bps")==4294967295 else port_rows.get((b,bp),{}).get("speed_bps"),
        })

    # Manual records retain their stable ID and endpoints. Discovery may add
    # evidence, but it never edits or replaces a manual record.
    manual = []
    for row in list(manual_links) + list(legacy_links):
        item = dict(row)
        if "local_port" in item and "source_port" not in item:
            item["source_port"] = item.pop("local_port")
        if "remote_port" in item and "target_port" not in item:
            item["target_port"] = item.pop("remote_port")
        item.setdefault("status", "VERIFIED" if item.get("source")=="VERIFIED" else "MANUAL")
        item.setdefault("source", "MANUAL")
        metadata = item.get("metadata") or {}
        source_port = normalize_interface(item.get("source_port"))
        target_port = normalize_interface(item.get("target_port"))
        source_channel = source_port.startswith(("po", "lag"))
        target_channel = target_port.startswith(("po", "lag"))
        explicit_type = str(item.get("link_type") or metadata.get("link_type") or "").upper().replace("-", "_")
        if source_channel or target_channel or explicit_type in ("PORT_CHANNEL", "LACP", "LAG"):
            item["link_type"] = "PORT_CHANNEL"
            item["port_channel"] = (item.get("port_channel") or metadata.get("port_channel")
                                    or (item.get("source_port") if source_channel else None)
                                    or (item.get("target_port") if target_channel else None))
        else:
            item.setdefault("link_type", "PHYSICAL")
            item["port_channel"] = item.get("port_channel") or metadata.get("port_channel")
        key = tuple(sorted(((int(item["source_device_id"]), normalize_interface(item["source_port"])),
                            (int(item["target_device_id"]), normalize_interface(item["target_port"])))))
        matching = None
        for x in links:
            candidate_key = tuple(sorted(((int(x["source_device_id"]), normalize_interface(x["source_port"])),
                                          (int(x["target_device_id"]), normalize_interface(x["target_port"])))) )
            if candidate_key == key:
                matching = x
                break
        if matching:
            item["discovery_evidence"] = matching["observations"]
            item["discovery_agrees"] = matching["status"] == "CONFIRMED"
            item["verification_status"]=matching["status"]
            for field in ('evidence_count','last_seen','freshness','source_oper_status','target_oper_status','source_speed_bps','target_speed_bps'):
                evidence_field=field
                if int(item['source_device_id'])!=int(matching['source_device_id']):
                    if field.startswith('source_'):evidence_field='target_'+field[7:]
                    elif field.startswith('target_'):evidence_field='source_'+field[7:]
                item[field]=matching.get(evidence_field)
            links.remove(matching)
        else:
            occupied = {(int(item["source_device_id"]), normalize_interface(item["source_port"])),
                        (int(item["target_device_id"]), normalize_interface(item["target_port"]))}
            for candidate in links:
                endpoints = {(int(candidate["source_device_id"]), normalize_interface(candidate["source_port"])),
                             (int(candidate["target_device_id"]), normalize_interface(candidate["target_port"]))}
                if occupied & endpoints:
                    candidate["status"] = "CONFLICT"
                    item["discovery_conflict"] = True
                    item["status"] = "CONFLICT"
        manual.append(item)
    all_links = manual + links
    manual_port_channels = set()
    for x in manual:
        channel = x.get("port_channel") or (x.get("metadata") or {}).get("port_channel")
        if not channel:
            continue
        devices = tuple(sorted((int(x["source_device_id"]), int(x["target_device_id"]))))
        source_channel = normalize_interface(x.get("source_port")).startswith(("po", "lag"))
        target_channel = normalize_interface(x.get("target_port")).startswith(("po", "lag"))
        if source_channel or target_channel or x.get("link_type") == "PORT_CHANNEL":
            endpoints = tuple(sorted((normalize_interface(x.get("source_port")), normalize_interface(x.get("target_port")))))
            manual_port_channels.add((devices, endpoints))
        else:
            # Several physical members may belong to one named channel.
            manual_port_channels.add((devices, str(channel).casefold()))
    logical_port_channels = {x["id"] for x in links if x["link_type"] == "PORT_CHANNEL"}
    return {"links": all_links, "unresolved": unresolved,
            "switches_checked": len(by_device),
            "physical_links": sum(x.get("link_type") == "PHYSICAL" for x in all_links),
            "confirmed_links": sum(x.get("verification_status",x["status"]) == "CONFIRMED" for x in all_links),
            "unverified_links": sum(x["status"] in ("ONE_SIDED", "UNKNOWN_UNVERIFIED") for x in all_links),
            "unresolved_observations": len(unresolved),
            "confirmed_physical_links":sum(x.get("link_type")=="PHYSICAL" and x.get("verification_status",x["status"])=="CONFIRMED" for x in all_links),
            "unverified_port_channels":sum(x.get("link_type")=="PORT_CHANNEL" and x.get("verification_status",x["status"]) not in ("CONFIRMED","VERIFIED") for x in all_links),
            "conflict_links": sum(x["status"] == "CONFLICT" for x in all_links),
            "port_channels": len(manual_port_channels | logical_port_channels)}
