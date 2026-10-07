"""Backfill the usage counters for a window when they were not running, from what AWS kept.

    python playground/backfill.py --start 2026-10-07T22:11:33Z --end 2026-10-07T23:37:10Z \
        --exclude-launch a2aa9823 --exclude-launch dbf0e1a9 --dry-run

Sources:
  AWS WAF sampled requests   every request to the distribution (when the population is small enough to be
                             sampled in full), with time, method, path, country, client IP and user agent.
                             WAF keeps them for 3 hours only, so run this within 3 hours of the window.
  ListMicrovms / GetMicrovm  the strands-box launches in the window; playground launches carry its lifetime
                             cap (SBX_MAX_DURATION_S, and that minus 60 for leases).

Rules: only requests from real browsers count (curl, headless Chrome, Python and link-preview bots are
dropped); a visitor is one client IP that loaded the page or called the API; a key user is a visitor that
sent the right key. Task, fan-out, lease and launch calls are counted from their POSTs. Request bodies are
not sampled, so decisions, denials, commands and fan-out sizes cannot be recovered and stay out.
"""

import argparse
import collections
import datetime as dt
import hashlib
import os
import re
import sys

import boto3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stats import Stats  # noqa: E402

BOT = re.compile(r"bot|preview|facebookexternalhit|slack|discord|whatsapp|telegram|linkedin|twitter|embed|crawler|"
                 r"spider|curl|python|headless|go-http|okhttp", re.I)


def ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def waf_requests(acl_arn: str, start, end) -> tuple[list, int, int]:
    waf = boto3.client("wafv2", region_name="us-east-1")
    name = acl_arn.split("/")[-2]
    out, pop, got = [], 0, 0
    for metric in (name, "api-writes-per-ip", "all-requests-per-ip", "aws-ip-reputation", "aws-common"):
        r = waf.get_sampled_requests(WebAclArn=acl_arn, RuleMetricName=metric, Scope="CLOUDFRONT",
                                     TimeWindow={"StartTime": start, "EndTime": end}, MaxItems=500)
        pop += r.get("PopulationSize", 0)
        got += len(r["SampledRequests"])
        for s in r["SampledRequests"]:
            q = s["Request"]
            hdr = {h["Name"].lower(): h["Value"] for h in q.get("Headers", [])}
            out.append({"t": s["Timestamp"], "method": q.get("Method"), "path": q["URI"].split("?")[0],
                        "country": q.get("Country"), "ip": hashlib.sha256(q["ClientIP"].encode()).hexdigest()[:16],
                        "browser": not BOT.search(hdr.get("user-agent", "")),
                        "key_ok": (hdr["x-playground-key"] == os.environ.get("PLAYGROUND_KEY"))
                        if "x-playground-key" in hdr else None})
    return out, pop, got


def launches(start, end, caps: set, exclude: set) -> list:
    sys.path.insert(0, os.path.expanduser("~/microvm-ctl"))
    from microvm import PlaneConfig, microvm_client
    cfg = PlaneConfig()
    api = microvm_client(cfg.region, cfg.profile)
    vms, token = [], None
    while True:
        r = api.list_microvms(**({"nextToken": token} if token else {}))
        vms += r.get("microvms", r.get("items", []))
        token = r.get("nextToken")
        if not token:
            break
    out = []
    for v in vms:
        vid = v.get("microvmId") or v.get("id")
        d = api.get_microvm(microvmIdentifier=vid)
        started = d.get("startedAt") or d.get("createdAt")
        if not started or not (start <= started <= end) or "strands-box" not in str(d.get("imageArn", "")):
            continue
        if d.get("maximumDurationInSeconds") in caps and not any(vid.startswith(f"microvm-{x}") for x in exclude):
            out.append({"id": vid, "t": started, "lease": d.get("maximumDurationInSeconds") != max(caps)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--acl-arn", required=True)
    ap.add_argument("--max-duration", type=int, default=900)
    ap.add_argument("--exclude-ip", action="append", default=[], help="sha256(ip)[:16] of a client to drop")
    ap.add_argument("--exclude-launch", action="append", default=[], help="first 8 hex of a microVM id to drop")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    start, end = ts(a.start), ts(a.end)

    reqs, population, sampled = waf_requests(a.acl_arn, start, end)
    if sampled < population:
        print(f"WAF sampled {sampled} of {population} requests; the counts below would be a lower bound", file=sys.stderr)
    by_ip = collections.defaultdict(list)
    for r in reqs:
        if r["browser"] and r["ip"] not in a.exclude_ip:
            by_ip[r["ip"]].append(r)

    c = collections.Counter()
    visitors = key_users = 0
    for rs in by_ip.values():
        loads = [r for r in rs if r["method"] == "GET" and r["path"] in ("/", "/index.html")]
        if not loads and not any(r["path"].startswith("/api/") for r in rs):
            continue
        visitors += 1
        key_users += any(r["key_ok"] is True for r in rs)
        for r in loads:
            hour = r["t"].astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H")
            c["page_loads"] += 1
            c[f"hour#{hour}#page_loads"] += 1
            if r["country"]:
                c[f"country#{r['country']}"] += 1
        for r in rs:
            hour = r["t"].astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H")
            if r["key_ok"] is False:
                c["wrong_keys"] += 1
            if r["method"] == "POST" and r["path"] == "/api/task" and r["key_ok"]:
                for k in ("tasks", "boxes"):
                    c[k] += 1
                    c[f"hour#{hour}#{k}"] += 1
            if r["method"] == "POST" and r["path"] == "/api/dispatch" and r["key_ok"]:
                c["fanouts"] += 1
    c["unique_visitors"] = visitors
    c["unique_key_users"] = key_users

    vms = launches(start, end, {a.max_duration, a.max_duration - 60}, set(a.exclude_launch))
    c["vms_launched"] = len(vms)
    c["leases"] = sum(1 for v in vms if v["lease"])
    c[f"backfill#{a.start}..{a.end}"] = 1

    for k, v in sorted(c.items()):
        print(f"{k:45} {v}")
    if a.dry_run:
        print("dry run: nothing written")
        return 0
    Stats(boto3.Session()).add(dict(c))
    print(f"added to {os.environ.get('SBX_STATS_TABLE')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
