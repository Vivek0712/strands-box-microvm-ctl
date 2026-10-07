"""Usage counters for the playground: one DynamoDB item per counter, updated with ADD so concurrent Lambda
instances never lose a count. Without SBX_STATS_TABLE the counters live in memory (local runs).

What is stored is aggregate only: counts per metric, per hour, per policy rule, per command name (the first
word of a step, never its arguments), per country (from CloudFront), and a hash of each visitor id so a
returning browser is not counted twice. No IP address, key, prompt, file content or full command is kept.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time

TABLE = os.environ.get("SBX_STATS_TABLE")
PK = "stats"
_NAME = re.compile(r"[^a-z0-9._-]")


def command_name(step: str) -> str | None:
    """The program a scripted step runs: `list` -> ls, `read:x` -> cat, `run:grep -n x` -> grep."""
    kind, _, arg = step.partition(":")
    if kind == "list":
        return "ls"
    if kind == "read":
        return "cat"
    if kind != "run" or not arg.strip():
        return None
    word = arg.strip().split()[0].lower()
    word = _NAME.sub("", word)[:20]
    return word or None


class Stats:
    def __init__(self, session=None):
        self._lock = threading.Lock()
        self._mem: dict = {}
        self._seen: set = set()
        self._ddb = None
        if TABLE:
            import boto3
            self._ddb = (session or boto3).client("dynamodb")

    # ── writes ───────────────────────────────────────────────────────────────────────────────────
    def add(self, counts: dict) -> None:
        counts = {k: v for k, v in counts.items() if v}
        if not counts:
            return
        if not self._ddb:
            with self._lock:
                for k, v in counts.items():
                    self._mem[k] = self._mem.get(k, 0) + v
            return
        for k, v in counts.items():
            try:
                self._ddb.update_item(TableName=TABLE, Key={"pk": {"S": PK}, "sk": {"S": k}},
                                      UpdateExpression="ADD n :v", ExpressionAttributeValues={":v": {"N": str(v)}})
            except Exception:  # a lost counter must never fail the request it describes
                pass

    def minimum(self, name: str, value: float) -> None:
        """Keep the smallest value seen (fastest box)."""
        if value is None:
            return
        if not self._ddb:
            with self._lock:
                if name not in self._mem or value < self._mem[name]:
                    self._mem[name] = value
            return
        try:
            self._ddb.update_item(TableName=TABLE, Key={"pk": {"S": PK}, "sk": {"S": name}},
                                  UpdateExpression="SET n = :v",
                                  ConditionExpression="attribute_not_exists(n) OR n > :v",
                                  ExpressionAttributeValues={":v": {"N": str(round(value, 1))}})
        except Exception:
            pass

    def visitor(self, visitor_id: str | None, kind: str) -> None:
        """Count a browser once per kind ('visitor' for anyone, 'key_user' for one that entered the key)."""
        if not visitor_id or len(visitor_id) > 64:
            return
        h = hashlib.sha256(f"{kind}:{visitor_id}".encode()).hexdigest()[:24]
        with self._lock:  # this instance has already counted or checked it
            if (kind, h) in self._seen:
                return
            self._seen.add((kind, h))
        if not self._ddb:
            self.add({f"unique_{kind}s": 1})
            return
        try:
            self._ddb.put_item(TableName=TABLE, Item={"pk": {"S": f"{kind}#{h}"}, "sk": {"S": "first"},
                                                      "at": {"N": str(int(time.time()))}},
                               ConditionExpression="attribute_not_exists(pk)")
        except Exception:
            return  # seen before, or the table refused: either way, do not count twice
        self.add({f"unique_{kind}s": 1})

    def event(self, name: str, n: int = 1, **extra) -> None:
        hour = time.strftime("%Y-%m-%dT%H", time.gmtime())
        self.add({name: n, f"hour#{hour}#{name}": n, **extra})

    # ── reads ────────────────────────────────────────────────────────────────────────────────────
    def _all(self) -> dict:
        if not self._ddb:
            with self._lock:
                return dict(self._mem)
        out, kwargs = {}, {"TableName": TABLE, "KeyConditionExpression": "pk = :p",
                           "ExpressionAttributeValues": {":p": {"S": PK}}}
        while True:
            page = self._ddb.query(**kwargs)
            for it in page.get("Items", []):
                out[it["sk"]["S"]] = float(it["n"]["N"])
            if "LastEvaluatedKey" not in page:
                return out
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def summary(self) -> dict:
        raw = self._all()
        num = lambda k: int(raw.get(k, 0))
        top = lambda prefix, n=8: sorted(((k[len(prefix):], int(v)) for k, v in raw.items() if k.startswith(prefix)),
                                         key=lambda kv: -kv[1])[:n]
        now = time.time()
        hours = []
        for i in range(23, -1, -1):
            h = time.strftime("%Y-%m-%dT%H", time.gmtime(now - i * 3600))
            hours.append({"hour": h[-2:] + ":00", "boxes": num(f"hour#{h}#boxes"), "tasks": num(f"hour#{h}#tasks")})
        boxes, decisions = num("boxes"), num("decisions")
        return {
            "totals": {
                "visitors": num("unique_visitors"), "key_users": num("unique_key_users"),
                "page_loads": num("page_loads"), "wrong_keys": num("wrong_keys"),
                "tasks": num("tasks"), "model_tasks": num("model_tasks"), "boxes": boxes,
                "decisions": decisions, "permits": num("permits"), "denials": num("denials"),
                "fanouts": num("fanouts"), "leases": num("leases"), "vms_launched": num("vms_launched"),
                "budget_refusals": num("budget_refusals"), "foreign_vm_attempts": num("foreign_vm_attempts"),
                "box_seconds": round(raw.get("box_ms_total", 0) / 1000, 1),
                "fastest_box_ms": raw.get("fastest_box_ms"),
                "deny_rate": round(num("denials") / decisions, 3) if decisions else None,
            },
            "denials_by_rule": top("rule#"),
            "commands": top("cmd#", 10),
            "countries": top("country#", 10),
            "last_24h": hours,
            "backfilled": sorted(k[len("backfill#"):] for k in raw if k.startswith("backfill#")),
        }
