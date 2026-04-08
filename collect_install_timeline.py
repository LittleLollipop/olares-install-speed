#!/usr/bin/env python3
import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from dateutil import parser as dtparser

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except Exception:
    plt = None

try:
    from kubernetes import client, config
    from kubernetes.client import ApiException
except Exception as e:
    print(f"ERROR: failed to import kubernetes python client: {e}", file=sys.stderr)
    print("Install deps: pip install -r speed/requirements.txt", file=sys.stderr)
    raise


APP_GROUP = "app.bytetrade.io"
APP_VERSION = "v1alpha1"
APP_PLURAL_APP_MGR = "applicationmanagers"
APP_PLURAL_APP = "applications"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    return dtparser.isoparse(ts)


def dt_to_iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def duration_seconds(a: Optional[datetime], b: Optional[datetime]) -> Optional[float]:
    if not a or not b:
        return None
    return (b - a).total_seconds()


def safe_get(d: Any, path: List[Any]) -> Any:
    cur = d
    for p in path:
        if cur is None:
            return None
        if isinstance(p, int):
            if not isinstance(cur, list) or p >= len(cur):
                return None
            cur = cur[p]
        else:
            if not isinstance(cur, dict) or p not in cur:
                return None
            cur = cur[p]
    return cur


@dataclass
class Phase:
    name: str
    enter_time: Optional[datetime]
    exit_time: Optional[datetime]


def load_kube_config() -> None:
    try:
        config.load_kube_config()
        return
    except Exception:
        pass
    try:
        config.load_incluster_config()
    except Exception as e:
        raise RuntimeError(f"cannot load kube config: {e}")


def get_custom_object(co_api: client.CustomObjectsApi, name: str, namespace: Optional[str], plural: str) -> Dict[str, Any]:
    if namespace:
        return co_api.get_namespaced_custom_object(APP_GROUP, APP_VERSION, namespace, plural, name)
    return co_api.get_cluster_custom_object(APP_GROUP, APP_VERSION, plural, name)


def list_custom_objects(co_api: client.CustomObjectsApi, namespace: Optional[str], plural: str) -> Dict[str, Any]:
    if namespace:
        return co_api.list_namespaced_custom_object(APP_GROUP, APP_VERSION, namespace, plural)
    return co_api.list_cluster_custom_object(APP_GROUP, APP_VERSION, plural)


def guess_appmgr_name(app: str, owner: str, namespace: str) -> List[str]:
    # Naming differs across versions; provide a small set of common guesses.
    # Keep order from most likely to least likely.
    return [
        f"{app}-{owner}-{namespace}",
        f"{app}-{owner}-{app}",
        f"{app}-{owner}",
        f"{app}-{namespace}-{owner}",
    ]


def extract_phases_from_appmgr(am: Dict[str, Any]) -> Tuple[str, List[Phase], Dict[str, Any]]:
    status = am.get("status", {}) or {}
    history = status.get("statusHistory") or status.get("history") or []

    # Try to interpret history items as {state, statusTime/updateTime/opTime/...}
    phases: List[Phase] = []
    normalized: List[Tuple[str, Optional[datetime]]] = []
    for item in history:
        state = item.get("state") or item.get("status") or item.get("name")
        t = item.get("statusTime") or item.get("updateTime") or item.get("opTime") or item.get("time")
        if state:
            normalized.append((str(state), parse_ts(t) if isinstance(t, str) else None))

    # Fallback: use current status state/time only.
    if not normalized:
        cur_state = status.get("state")
        t = status.get("statusTime") or status.get("updateTime") or status.get("opTime")
        if cur_state:
            normalized = [(str(cur_state), parse_ts(t) if isinstance(t, str) else None)]

    # De-dup consecutive same state, keep earliest enter time.
    dedup: List[Tuple[str, Optional[datetime]]] = []
    for st, t in normalized:
        if dedup and dedup[-1][0] == st:
            continue
        dedup.append((st, t))

    for i, (st, enter) in enumerate(dedup):
        exit_t = dedup[i + 1][1] if i + 1 < len(dedup) else None
        phases.append(Phase(name=st, enter_time=enter, exit_time=exit_t))

    op_id = safe_get(status, ["opID"]) or safe_get(status, ["opId"]) or ""
    return str(op_id), phases, status


def pod_time_from_condition(pod: client.V1Pod, cond_type: str, cond_status: str = "True") -> Optional[datetime]:
    if not pod.status or not pod.status.conditions:
        return None
    for c in pod.status.conditions:
        if c.type == cond_type and c.status == cond_status and c.last_transition_time:
            return c.last_transition_time
    return None


def normalize_k8s_event_reason(reason: Optional[str]) -> str:
    return (reason or "").strip()


def list_pods_for_app(v1: client.CoreV1Api, namespace: str, app: Optional[str], owner: Optional[str]) -> List[client.V1Pod]:
    # app-service installed workloads typically have labels:
    # - constants.ApplicationNameLabel
    # - constants.ApplicationOwnerLabel
    # We do not hardcode label keys here; allow user to provide only namespace and we will filter by heuristics.
    pods = v1.list_namespaced_pod(namespace=namespace).items
    if not app and not owner:
        return pods

    ret: List[client.V1Pod] = []
    for p in pods:
        labels = p.metadata.labels or {}
        if app and (labels.get("app.bytetrade.io/name") == app or labels.get("bytetrade.io/app") == app or labels.get("app") == app):
            if owner:
                if labels.get("app.bytetrade.io/owner") == owner or labels.get("bytetrade.io/owner") == owner or labels.get("owner") == owner:
                    ret.append(p)
                else:
                    # if owner label missing, still keep if app matches strongly
                    ret.append(p)
            else:
                ret.append(p)
            continue
        # weaker match: label contains app name
        if app:
            for k, v in labels.items():
                if isinstance(v, str) and v == app and ("app" in k or "application" in k):
                    ret.append(p)
                    break
    return ret


def list_events_for_pod(v1: client.CoreV1Api, namespace: str, pod: client.V1Pod) -> List[client.CoreV1Event]:
    field_sel = f"involvedObject.kind=Pod,involvedObject.name={pod.metadata.name}"
    try:
        return v1.list_namespaced_event(namespace=namespace, field_selector=field_sel).items
    except ApiException:
        return []


def event_time(ev: client.CoreV1Event) -> Optional[datetime]:
    # Prefer eventTime (newer), then lastTimestamp, then firstTimestamp.
    for attr in ["event_time", "last_timestamp", "first_timestamp"]:
        t = getattr(ev, attr, None)
        if t:
            return t
    if ev.metadata and ev.metadata.creation_timestamp:
        return ev.metadata.creation_timestamp
    return None


def find_first_event_time(events: List[client.CoreV1Event], reason: str) -> Optional[datetime]:
    times = []
    for e in events:
        if normalize_k8s_event_reason(e.reason) == reason:
            t = event_time(e)
            if t:
                times.append(t)
    return min(times) if times else None


def find_last_event_time(events: List[client.CoreV1Event], reason: str) -> Optional[datetime]:
    times = []
    for e in events:
        if normalize_k8s_event_reason(e.reason) == reason:
            t = event_time(e)
            if t:
                times.append(t)
    return max(times) if times else None


def compute_bottlenecks(phases: List[Dict[str, Any]], pods: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    bottlenecks: List[Dict[str, Any]] = []

    for ph in phases:
        d = ph.get("duration_seconds")
        if d is not None and d >= 120:
            bottlenecks.append({"type": "phase_slow", "phase": ph["name"], "duration_seconds": d})

    for p in pods:
        if p.get("schedule_duration_seconds") is not None and p["schedule_duration_seconds"] >= 30:
            bottlenecks.append({"type": "pod_schedule_slow", "pod": p["name"], "duration_seconds": p["schedule_duration_seconds"]})
        if p.get("pull_duration_seconds") is not None and p["pull_duration_seconds"] >= 60:
            bottlenecks.append({"type": "pod_pull_slow", "pod": p["name"], "duration_seconds": p["pull_duration_seconds"]})
        if p.get("start_to_ready_seconds") is not None and p["start_to_ready_seconds"] >= 60:
            bottlenecks.append({"type": "pod_startup_slow", "pod": p["name"], "duration_seconds": p["start_to_ready_seconds"]})

    return bottlenecks


def render_phase_pie(phases: List[Dict[str, Any]], out_path: str, title: str) -> None:
    if plt is None:
        raise RuntimeError("matplotlib not available, install: pip install -r speed/requirements.txt")

    labels: List[str] = []
    sizes: List[float] = []
    for ph in phases:
        d = ph.get("duration_seconds")
        if d is None:
            continue
        if d <= 0:
            continue
        labels.append(str(ph.get("name") or "unknown"))
        sizes.append(float(d))

    if not sizes:
        raise RuntimeError("no phase durations to render pie (missing duration_seconds)")

    fig = plt.figure(figsize=(8, 6), dpi=160)
    ax = fig.add_subplot(111)
    ax.set_title(title)
    ax.pie(
        sizes,
        labels=labels,
        autopct=lambda p: f"{p:.1f}%",
        startangle=90,
        counterclock=False,
    )
    ax.axis("equal")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--namespace", required=True, help="Kubernetes namespace where the app is installed")
    ap.add_argument("--appmgr", help="ApplicationManager CR name")
    ap.add_argument("--app", help="App name (used to guess appmgr name and filter pods)")
    ap.add_argument("--owner", help="Owner username (used to guess appmgr name and filter pods)")
    ap.add_argument("--out", required=True, help="Output JSON file path")
    ap.add_argument("--csv", help="Optional output CSV file path (timeline rows)")
    ap.add_argument("--pie", help="Optional output PNG path for phase pie chart")
    args = ap.parse_args()

    load_kube_config()
    co = client.CustomObjectsApi()
    v1 = client.CoreV1Api()

    namespace = args.namespace

    appmgr_name = args.appmgr
    am_obj: Optional[Dict[str, Any]] = None

    if appmgr_name:
        try:
            am_obj = get_custom_object(co, appmgr_name, None, APP_PLURAL_APP_MGR)
        except Exception as e:
            print(f"ERROR: failed to get ApplicationManager '{appmgr_name}': {e}", file=sys.stderr)
            return 2
    else:
        if not args.app or not args.owner:
            print("ERROR: provide --appmgr or (--app and --owner)", file=sys.stderr)
            return 2
        candidates = guess_appmgr_name(args.app, args.owner, namespace)
        last_err = None
        for c in candidates:
            try:
                am_obj = get_custom_object(co, c, None, APP_PLURAL_APP_MGR)
                appmgr_name = c
                break
            except Exception as e:
                last_err = e
        if not am_obj:
            print(f"ERROR: cannot find ApplicationManager by guesses: {candidates}", file=sys.stderr)
            print(f"Last error: {last_err}", file=sys.stderr)
            return 2

    op_id, phase_objs, am_status = extract_phases_from_appmgr(am_obj)
    phases_out: List[Dict[str, Any]] = []
    for ph in phase_objs:
        phases_out.append(
            {
                "name": ph.name,
                "enter_time": dt_to_iso(ph.enter_time),
                "exit_time": dt_to_iso(ph.exit_time),
                "duration_seconds": duration_seconds(ph.enter_time, ph.exit_time),
            }
        )

    # Pods
    pods = list_pods_for_app(v1, namespace, args.app, args.owner)
    pods_out: List[Dict[str, Any]] = []
    containers_out: List[Dict[str, Any]] = []
    for pod in pods:
        created = pod.metadata.creation_timestamp
        scheduled = pod_time_from_condition(pod, "PodScheduled", "True")
        ready = pod_time_from_condition(pod, "Ready", "True")

        evs = list_events_for_pod(v1, namespace, pod)
        pulling_first = find_first_event_time(evs, "Pulling")
        pulled_last = find_last_event_time(evs, "Pulled")
        created_ev = find_first_event_time(evs, "Created")
        started_ev = find_first_event_time(evs, "Started")

        # Container startedAt
        if pod.status and pod.status.container_statuses:
            for cs in pod.status.container_statuses:
                started_at = None
                if cs.state and cs.state.running and cs.state.running.started_at:
                    started_at = cs.state.running.started_at
                containers_out.append(
                    {
                        "pod": pod.metadata.name,
                        "container": cs.name,
                        "image": cs.image,
                        "started_at": dt_to_iso(started_at),
                        "ready": bool(cs.ready),
                        "restart_count": int(cs.restart_count or 0),
                    }
                )

        pod_row = {
            "name": pod.metadata.name,
            "node": pod.spec.node_name,
            "created_at": dt_to_iso(created),
            "scheduled_at": dt_to_iso(scheduled),
            "pulling_first_at": dt_to_iso(pulling_first),
            "pulled_last_at": dt_to_iso(pulled_last),
            "created_event_at": dt_to_iso(created_ev),
            "started_event_at": dt_to_iso(started_ev),
            "ready_at": dt_to_iso(ready),
            "schedule_duration_seconds": duration_seconds(created, scheduled),
            "pull_duration_seconds": duration_seconds(pulling_first, pulled_last),
            "start_to_ready_seconds": duration_seconds(started_ev, ready),
        }
        pods_out.append(pod_row)

    result = {
        "collected_at": dt_to_iso(now_utc()),
        "namespace": namespace,
        "appmgr": {
            "name": appmgr_name,
            "op_id": op_id,
            "status_state": safe_get(am_status, ["state"]),
            "status_progress": safe_get(am_status, ["progress"]),
            "status_message": safe_get(am_status, ["message"]),
        },
        "appmgr_phases": phases_out,
        "pods": pods_out,
        "containers": containers_out,
    }
    result["bottlenecks"] = compute_bottlenecks(phases_out, pods_out)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"OK: wrote JSON to {args.out}")

    if args.pie:
        render_phase_pie(
            phases_out,
            args.pie,
            title=f"Install phase share  appmgr={appmgr_name}  ns={namespace}",
        )
        print(f"OK: wrote pie chart to {args.pie}")

    if args.csv:
        os.makedirs(os.path.dirname(args.csv), exist_ok=True)
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["kind", "name", "step", "time_iso"])
            for ph in phases_out:
                if ph["enter_time"]:
                    w.writerow(["appmgr", appmgr_name, f"enter:{ph['name']}", ph["enter_time"]])
                if ph["exit_time"]:
                    w.writerow(["appmgr", appmgr_name, f"exit:{ph['name']}", ph["exit_time"]])
            for p in pods_out:
                for step, k in [
                    ("created", "created_at"),
                    ("scheduled", "scheduled_at"),
                    ("pulling_first", "pulling_first_at"),
                    ("pulled_last", "pulled_last_at"),
                    ("created_event", "created_event_at"),
                    ("started_event", "started_event_at"),
                    ("ready", "ready_at"),
                ]:
                    if p.get(k):
                        w.writerow(["pod", p["name"], step, p[k]])
            for c in containers_out:
                if c.get("started_at"):
                    w.writerow(["container", f"{c['pod']}/{c['container']}", "startedAt", c["started_at"]])
        print(f"OK: wrote CSV to {args.csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

