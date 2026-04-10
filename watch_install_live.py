#!/usr/bin/env python3
import argparse
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from dateutil import parser as dtparser
import re

try:
    from kubernetes import client, config
    from kubernetes.client import ApiException
except Exception as e:
    print(f"ERROR: failed to import kubernetes python client: {e}", file=sys.stderr)
    print("Install deps: pip install -r requirements.txt", file=sys.stderr)
    raise

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.table import Table
    from rich.text import Text
    from rich.style import Style
except Exception as e:
    print(f"ERROR: failed to import rich: {e}", file=sys.stderr)
    print("Install deps: pip install -r requirements.txt", file=sys.stderr)
    raise


APP_GROUP = "app.bytetrade.io"
APP_VERSION = "v1alpha1"
APP_PLURAL_APP_MGR = "applicationmanagers"
OPTYPE_INSTALL = "InstallOp"

PIE_PALETTE = [
    "cyan",
    "magenta",
    "green",
    "yellow",
    "blue",
    "bright_cyan",
    "bright_magenta",
    "bright_green",
    "bright_yellow",
    "bright_blue",
]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    return dtparser.isoparse(ts)


def normalize_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Compare API timestamps reliably (naive vs aware)."""
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def spec_matches_app(want: str, spec: Dict[str, Any]) -> bool:
    """Match spec.appName or spec.rawAppName (case-insensitive fallback)."""
    want_n = (want or "").strip()
    if not want_n:
        return False
    for key in ("appName", "rawAppName"):
        v = str(spec.get(key) or "").strip()
        if not v:
            continue
        if v == want_n or v.lower() == want_n.lower():
            return True
    return False


def spec_matches_namespace_filter(filter_ns: Optional[str], spec: Dict[str, Any]) -> bool:
    """If --namespace is set, require spec.appNamespace to match (user workload namespace)."""
    if not filter_ns or not str(filter_ns).strip():
        return True
    return str(spec.get("appNamespace") or "") == str(filter_ns).strip()


def dt_to_iso(dt: Optional[datetime]) -> str:
    if not dt:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def dur_s(a: Optional[datetime], b: Optional[datetime]) -> Optional[float]:
    if not a or not b:
        return None
    return (b - a).total_seconds()


def fmt_dur(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.1f}s"
    m = int(seconds // 60)
    s = seconds - m * 60
    return f"{m}m{s:0.0f}s"


def load_kube_config() -> None:
    try:
        config.load_kube_config()
        return
    except Exception:
        pass
    config.load_incluster_config()


def get_custom_object(co_api: client.CustomObjectsApi, name: str) -> Dict[str, Any]:
    return co_api.get_cluster_custom_object(APP_GROUP, APP_VERSION, APP_PLURAL_APP_MGR, name)


def list_appmgrs(co_api: client.CustomObjectsApi) -> List[Dict[str, Any]]:
    ret = co_api.list_cluster_custom_object(APP_GROUP, APP_VERSION, APP_PLURAL_APP_MGR)
    return list(ret.get("items") or [])


def list_appmgrs_safe(co_api: client.CustomObjectsApi) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    try:
        return list_appmgrs(co_api), None
    except ApiException as e:
        body = (e.body or b"").decode("utf-8", errors="replace") if isinstance(e.body, (bytes, bytearray)) else str(e.body or "")
        return [], f"HTTP {e.status} {e.reason}: {body[:500]}"
    except Exception as e:
        return [], str(e)


def guess_appmgr_name(app: str, owner: str, namespace: str) -> List[str]:
    return [
        f"{app}-{owner}-{namespace}",
        f"{app}-{owner}-{app}",
        f"{app}-{owner}",
        f"{app}-{namespace}-{owner}",
    ]


def _appmgr_anchor_time(am: Dict[str, Any]) -> Optional[datetime]:
    """
    Latest relevant timestamp for this ApplicationManager: max of creationTimestamp
    and status opTime/statusTime/updateTime. InstallOp alone is unreliable after Running
    (opType may change); using max avoids missing a completed install.
    """
    times: List[datetime] = []
    meta = am.get("metadata") or {}
    ct = meta.get("creationTimestamp")
    if isinstance(ct, str):
        t = normalize_utc(parse_ts(ct))
        if t:
            times.append(t)
    status = am.get("status") or {}
    for key in ("opTime", "statusTime", "updateTime"):
        t_str = status.get(key)
        if isinstance(t_str, str):
            t = normalize_utc(parse_ts(t_str))
            if t:
                times.append(t)
    return max(times) if times else None


def pick_latest_appmgr_for_app(
    co: client.CustomObjectsApi,
    app: str,
    filter_namespace: Optional[str],
) -> Optional[Tuple[str, str, Optional[str]]]:
    """Latest ApplicationManager for spec.appName == app (by status/update time)."""
    items, _err = list_appmgrs_safe(co)
    best: Optional[Tuple[datetime, str, str, str]] = None
    for am in items:
        spec = am.get("spec") or {}
        if not spec_matches_app(app, spec):
            continue
        if not spec_matches_namespace_filter(filter_namespace, spec):
            continue
        t = _appmgr_anchor_time(am)
        if not t:
            continue
        name = str(((am.get("metadata") or {}).get("name")) or "")
        ns = str(((am.get("spec") or {}).get("appNamespace")) or "")
        if not name or not ns:
            continue
        am_owner = str(((am.get("spec") or {}).get("appOwner")) or "")
        cand = (t, name, ns, am_owner)
        if best is None or t > best[0]:
            best = cand
    if not best:
        return None
    _, name, ns, am_owner = best
    return name, ns, (am_owner or None)


def pick_appmgr_or_wait(
    co: client.CustomObjectsApi,
    appmgr: Optional[str],
    app: Optional[str],
    owner: Optional[str],
    namespace: Optional[str],
    arm_start: datetime,
    refresh: float,
    wait_new_install: bool,
    filter_namespace: Optional[str],
) -> Tuple[str, str, Optional[str]]:
    if appmgr:
        am = get_custom_object(co, appmgr)
        ns = str(((am.get("spec") or {}).get("appNamespace")) or (namespace or "-"))
        am_owner = str(((am.get("spec") or {}).get("appOwner")) or (owner or ""))
        return appmgr, ns, (am_owner or None)

    if not app:
        raise RuntimeError("provide --app, or --appmgr")

    # Deterministic guesses (optional fast path)
    if owner and namespace:
        for c in guess_appmgr_name(app, owner, namespace):
            try:
                am = get_custom_object(co, c)
                ns = str(((am.get("spec") or {}).get("appNamespace")) or namespace)
                am_owner = str(((am.get("spec") or {}).get("appOwner")) or owner)
                return c, ns, (am_owner or None)
            except Exception:
                pass

    # Arming mode: prefer an install that started after this process (t >= arm_start).
    # ApplicationManager CR is cluster-scoped; --namespace filters spec.appNamespace (user app namespace).
    wait_log = 0.0
    diag_log = 0.0
    arm_u = normalize_utc(arm_start) or arm_start
    while True:
        items, list_err = list_appmgrs_safe(co)
        if list_err:
            if time.time() - wait_log >= 5.0:
                wait_log = time.time()
                print(f"[install-speed] cannot list applicationmanagers: {list_err}", file=sys.stderr)
            time.sleep(refresh)
            continue

        best: Optional[Tuple[datetime, Dict[str, Any]]] = None
        for am in items:
            spec = am.get("spec") or {}
            status = am.get("status") or {}
            if not spec_matches_app(app, spec):
                continue
            if not spec_matches_namespace_filter(filter_namespace, spec):
                continue

            t = _appmgr_anchor_time(am)
            if not t or t < arm_u:
                continue

            # After install finishes, opType may no longer be InstallOp; --wait-new-install must still match.
            if not wait_new_install:
                if str(spec.get("opType") or "") != OPTYPE_INSTALL and str(status.get("opType") or "") != OPTYPE_INSTALL:
                    continue

            if best is None or t > best[0]:
                best = (t, am)

        if best:
            am = best[1]
            name = str(((am.get("metadata") or {}).get("name")) or "")
            ns = str(((am.get("spec") or {}).get("appNamespace")) or (namespace or ""))
            am_owner = str(((am.get("spec") or {}).get("appOwner")) or (owner or ""))
            if name and ns:
                print(
                    f"[install-speed] Using ApplicationManager created/updated after script start: {name} ns={ns}",
                    file=sys.stderr,
                )
                return name, ns, (am_owner or None)

        if not wait_new_install:
            fb = pick_latest_appmgr_for_app(co, app, filter_namespace)
            if fb:
                name, ns, am_owner = fb
                print(
                    f"[install-speed] No install started after this process; attaching to latest ApplicationManager: "
                    f"{name} ns={ns}. (Use --wait-new-install to only wait for a fresh install.)",
                    file=sys.stderr,
                )
                return name, ns, am_owner

        if time.time() - wait_log >= 5.0:
            wait_log = time.time()
            ns_hint = f" spec.appNamespace must equal {filter_namespace!r}" if (filter_namespace or "").strip() else ""
            print(
                f"[install-speed] Waiting: app matches appName/rawAppName {app!r}{ns_hint}, "
                f"anchor time >= script start ({dt_to_iso(arm_u)}). "
                f"Tip: kubectl get applicationmanagers -o wide ; or --namespace <user-namespace> ; or --appmgr NAME",
                file=sys.stderr,
            )

        if wait_new_install and time.time() - diag_log >= 25.0:
            diag_log = time.time()
            sample = []
            for am in items[:15]:
                sp = am.get("spec") or {}
                if spec_matches_app(app, sp) and spec_matches_namespace_filter(filter_namespace, sp):
                    st = sp.get("appName")
                    raw = sp.get("rawAppName")
                    ns = sp.get("appNamespace")
                    nm = (am.get("metadata") or {}).get("name")
                    ta = _appmgr_anchor_time(am)
                    sample.append(f"{nm} app={st!r} raw={raw!r} ns={ns!r} anchor={dt_to_iso(ta)}")
            print(f"[install-speed] cluster ApplicationManagers total={len(items)}", file=sys.stderr)
            if sample:
                print("[install-speed] rows matching name/ns filter but anchor < start (or stale):", file=sys.stderr)
                for line in sample[:10]:
                    print(f"  {line}", file=sys.stderr)
            else:
                ns_part = f" and spec.appNamespace={filter_namespace!r}" if (filter_namespace or "").strip() else ""
                print(
                    f"[install-speed] no row matches appName/rawAppName {app!r}{ns_part} — check spelling or pass --namespace",
                    file=sys.stderr,
                )

        time.sleep(refresh)


def pod_time_from_condition(pod: client.V1Pod, cond_type: str, cond_status: str = "True") -> Optional[datetime]:
    if not pod.status or not pod.status.conditions:
        return None
    for c in pod.status.conditions:
        if c.type == cond_type and c.status == cond_status and c.last_transition_time:
            return c.last_transition_time
    return None


def list_pods(v1: client.CoreV1Api, namespace: str) -> List[client.V1Pod]:
    try:
        return v1.list_namespaced_pod(namespace=namespace).items
    except ApiException as e:
        print(
            f"[install-speed] list_namespaced_pod ns={namespace!r} failed: {e.status} {e.reason}",
            file=sys.stderr,
        )
        return []
    except Exception as e:
        print(f"[install-speed] list_namespaced_pod ns={namespace!r} failed: {e}", file=sys.stderr)
        return []


def list_events_for_pod(v1: client.CoreV1Api, namespace: str, pod_name: str) -> List[client.CoreV1Event]:
    field_sel = f"involvedObject.kind=Pod,involvedObject.name={pod_name}"
    try:
        return v1.list_namespaced_event(namespace=namespace, field_selector=field_sel).items
    except Exception:
        return []


def event_time(ev: client.CoreV1Event) -> Optional[datetime]:
    for attr in ["event_time", "last_timestamp", "first_timestamp"]:
        t = getattr(ev, attr, None)
        if t:
            return t
    if ev.metadata and ev.metadata.creation_timestamp:
        return ev.metadata.creation_timestamp
    return None


def find_first_event_time(events: List[client.CoreV1Event], reason: str) -> Optional[datetime]:
    times: List[datetime] = []
    for e in events:
        if (e.reason or "").strip() == reason:
            t = event_time(e)
            if t:
                times.append(t)
    return min(times) if times else None


def find_last_event_time(events: List[client.CoreV1Event], reason: str) -> Optional[datetime]:
    times: List[datetime] = []
    for e in events:
        if (e.reason or "").strip() == reason:
            t = event_time(e)
            if t:
                times.append(t)
    return max(times) if times else None


def parse_container_from_event_message(msg: str) -> Optional[str]:
    # Common patterns:
    # - "Created container X"
    # - "Started container X"
    # - 'Created container "X"' (rare)
    m = re.search(r"\bcontainer\s+\"?([A-Za-z0-9_.-]+)\"?\b", msg)
    if m:
        return m.group(1)
    return None


def parse_image_from_event_message(msg: str) -> Optional[str]:
    # Common patterns:
    # - 'Pulling image "repo/name:tag"'
    # - 'Successfully pulled image "..."'
    m = re.search(r"\bimage\s+\"([^\"]+)\"", msg)
    if m:
        return m.group(1)
    return None


def build_container_event_times(
    events: List[client.CoreV1Event],
    pod: client.V1Pod,
) -> Dict[str, Dict[str, Optional[datetime]]]:
    """
    Returns per-container times:
      pulling_first, pulled_last, created_first, started_first
    Best-effort: pulling/pulled often don't include container name; we map by image when unique.
    """
    per: Dict[str, Dict[str, Optional[datetime]]] = {}
    images_to_containers: Dict[str, List[str]] = {}
    if pod.status and pod.status.container_statuses:
        for cs in pod.status.container_statuses:
            per.setdefault(cs.name, {"pulling_first": None, "pulled_last": None, "created_first": None, "started_first": None})
            images_to_containers.setdefault(cs.image, []).append(cs.name)

    def set_min(container: str, key: str, t: Optional[datetime]) -> None:
        if not t:
            return
        per.setdefault(container, {"pulling_first": None, "pulled_last": None, "created_first": None, "started_first": None})
        cur = per[container].get(key)
        if cur is None or t < cur:
            per[container][key] = t

    def set_max(container: str, key: str, t: Optional[datetime]) -> None:
        if not t:
            return
        per.setdefault(container, {"pulling_first": None, "pulled_last": None, "created_first": None, "started_first": None})
        cur = per[container].get(key)
        if cur is None or t > cur:
            per[container][key] = t

    for e in events:
        reason = (e.reason or "").strip()
        msg = e.message or ""
        t = event_time(e)
        c = parse_container_from_event_message(msg) if msg else None

        if reason in ("Created", "Started") and c:
            if reason == "Created":
                set_min(c, "created_first", t)
            else:
                set_min(c, "started_first", t)
            continue

        if reason in ("Pulling", "Pulled"):
            # Try container in message; otherwise map by image if unique.
            img = parse_image_from_event_message(msg) if msg else None
            targets: List[str] = []
            if c:
                targets = [c]
            elif img and img in images_to_containers and len(images_to_containers[img]) == 1:
                targets = images_to_containers[img]

            for target in targets:
                if reason == "Pulling":
                    set_min(target, "pulling_first", t)
                else:
                    set_max(target, "pulled_last", t)

    return per


WARNING_REASONS = {
    "FailedScheduling",
    "Failed",
    "BackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "CrashLoopBackOff",
    "FailedMount",
    "FailedAttachVolume",
    "FailedCreatePodSandBox",
}


def summarize_warning(events: List[client.CoreV1Event]) -> Dict[str, Any]:
    """
    Returns:
      latest_reason, latest_message, latest_time,
      first_by_reason{reason: time}
    """
    latest: Optional[Tuple[datetime, client.CoreV1Event]] = None
    first_by_reason: Dict[str, datetime] = {}

    for e in events:
        reason = (e.reason or "").strip()
        t = event_time(e)
        if not reason or not t:
            continue

        if reason in WARNING_REASONS or (getattr(e, "type", "") == "Warning"):
            if reason not in first_by_reason or t < first_by_reason[reason]:
                first_by_reason[reason] = t
            if latest is None or t > latest[0]:
                latest = (t, e)

    if not latest:
        return {"latest_reason": None, "latest_message": None, "latest_time": None, "first_by_reason": first_by_reason}

    _, ev = latest
    return {
        "latest_reason": (ev.reason or "").strip() or None,
        "latest_message": (ev.message or "").strip() or None,
        "latest_time": event_time(ev),
        "first_by_reason": first_by_reason,
    }


def filter_pods(pods: List[client.V1Pod], app: Optional[str], owner: Optional[str]) -> List[client.V1Pod]:
    if not app and not owner:
        return pods
    ret: List[client.V1Pod] = []
    for p in pods:
        labels = p.metadata.labels or {}
        app_hit = True
        owner_hit = True
        if app:
            app_hit = any(
                labels.get(k) == app
                for k in [
                    "app.bytetrade.io/name",
                    "bytetrade.io/app",
                    "app",
                    "app.kubernetes.io/name",
                    "app.kubernetes.io/instance",
                ]
            ) or any(("app" in k and labels.get(k) == app) for k in labels.keys())
        if owner:
            owner_hit = any(
                labels.get(k) == owner
                for k in [
                    "app.bytetrade.io/owner",
                    "bytetrade.io/owner",
                    "owner",
                ]
            )
        if app_hit and owner_hit:
            ret.append(p)
    return ret


def pick_pods_for_monitor(
    pods: List[client.V1Pod],
    app: Optional[str],
    owner: Optional[str],
) -> Tuple[List[client.V1Pod], str]:
    """
    Prefer strict label filter; if it yields nothing, fall back so the UI still shows workloads.
    Olares/Helm pods often omit app.bytetrade.io/name but names still contain the app id.
    """
    if not pods:
        return [], ""
    if not app and not owner:
        return pods, ""
    strict = filter_pods(pods, app, owner)
    if strict:
        return strict, ""
    if owner and app:
        loose = filter_pods(pods, app, None)
        if loose:
            return loose, "Pods: owner label not on workloads; dropped owner filter for pod list."
    if app:
        al = app.lower()
        by_name = [p for p in pods if al in (p.metadata.name or "").lower()]
        if by_name:
            return by_name, "Pods: labels did not match --app; showing pods whose name contains the app name."
        return pods, "Pods: labels did not match --app; showing all pods in app namespace (usually one app)."
    if owner:
        ol = owner.lower()
        by_name = [p for p in pods if ol in (p.metadata.name or "").lower()]
        if by_name:
            return by_name, "Pods: owner label missing; matched pod name."
        return pods, "Pods: owner label missing; showing all pods in namespace."
    return pods, ""


def summarize_appmgr(am: Dict[str, Any]) -> Tuple[str, str, str, Optional[datetime]]:
    status = am.get("status", {}) or {}
    state = str(status.get("state") or "-")
    progress = str(status.get("progress") or "-")
    msg = str(status.get("message") or "-")
    t = status.get("statusTime") or status.get("updateTime") or status.get("opTime")
    return state, progress, msg, parse_ts(t) if isinstance(t, str) else None


def update_phase_tracker(
    tracker: List[Tuple[str, datetime]],
    cur_state: str,
    state_time: Optional[datetime],
) -> None:
    # tracker: [(state, enter_time_utc), ...]
    if not cur_state or cur_state == "-":
        return
    if tracker and tracker[-1][0] == cur_state:
        return
    enter = state_time or now_utc()
    # Ensure monotonic-ish ordering
    if tracker and enter < tracker[-1][1]:
        enter = tracker[-1][1]
    tracker.append((cur_state, enter))


def render_phase_table(tracker: List[Tuple[str, datetime]]) -> Table:
    t = Table(show_header=True, header_style="bold", box=None, expand=True)
    t.add_column("Phase", overflow="ignore", min_width=12)
    t.add_column("Enter(UTC)", overflow="ignore", min_width=18)
    t.add_column("Duration", justify="right", width=10, overflow="ignore")
    now = now_utc()
    for i, (st, enter) in enumerate(tracker[-8:]):
        exit_t = tracker[i + 1][1] if i + 1 < len(tracker) else None
        dur = dur_s(enter, exit_t or now)
        t.add_row(st, dt_to_iso(enter), fmt_dur(dur))
    return t


def phase_durations_seconds(tracker: List[Tuple[str, datetime]]) -> List[Tuple[str, float]]:
    now = now_utc()
    out: List[Tuple[str, float]] = []
    for i, (st, enter) in enumerate(tracker):
        exit_t = tracker[i + 1][1] if i + 1 < len(tracker) else now
        d = dur_s(enter, exit_t) or 0.0
        if d < 0:
            d = 0.0
        out.append((st, float(d)))
    return out


def _pod_sched_pull_startready_seconds(
    p: client.V1Pod,
    pod_event_cache: Dict[str, Dict[str, Any]],
    started_at: datetime,
) -> Tuple[float, float, float]:
    """Per-pod: schedule delay, image pull window, first Started event -> Ready (seconds)."""
    created = p.metadata.creation_timestamp or started_at
    scheduled = pod_time_from_condition(p, "PodScheduled", "True")
    sched = float(dur_s(created, scheduled) or 0.0)
    if sched < 0:
        sched = 0.0

    pn = p.metadata.name or ""
    cache = pod_event_cache.get(pn) or {}
    pl = float(dur_s(cache.get("pod_pulling_first"), cache.get("pod_pulled_last")) or 0.0)
    if pl < 0:
        pl = 0.0
    sr = float(dur_s(cache.get("pod_started_first"), pod_time_from_condition(p, "Ready", "True")) or 0.0)
    if sr < 0:
        sr = 0.0
    return sched, pl, sr


def build_per_pod_track_slices(
    pods: List[client.V1Pod],
    pod_event_cache: Dict[str, Dict[str, Any]],
    started_at: datetime,
) -> List[Tuple[str, float]]:
    """
    One row per Pod: sched + pull + (Started->Ready) for that pod only.
    % are shares of the sum across pods (which pod dominates aggregate infra time).
    """
    rows: List[Tuple[str, float]] = []
    for p in pods:
        pn = p.metadata.name or "?"
        a, b, c = _pod_sched_pull_startready_seconds(p, pod_event_cache, started_at)
        total = a + b + c
        if total > 0.001:
            rows.append((pn, total))
    rows.sort(key=lambda x: -x[1])
    return rows


def build_per_container_pull_slices(
    pods: List[client.V1Pod],
    pod_event_cache: Dict[str, Dict[str, Any]],
) -> List[Tuple[str, float]]:
    """
    One row per container: Pulling -> Pulled from events (which image pull is slowest).
    If events only have pod-level pull times, one fallback row per pod: "pod (pod pull)".
    """
    rows: List[Tuple[str, float]] = []
    pods_with_ctr_pull: set[str] = set()
    for p in pods:
        pn = p.metadata.name or "?"
        per_ct: Dict[str, Dict[str, Optional[datetime]]] = getattr(
            p, "_per_container_event_times", {}
        )  # type: ignore[attr-defined]
        if not p.status or not p.status.container_statuses:
            continue
        for cs in p.status.container_statuses:
            ce = per_ct.get(cs.name) or {}
            d = dur_s(ce.get("pulling_first"), ce.get("pulled_last"))
            if d is not None and d > 0.001:
                rows.append((f"{pn}/{cs.name}", float(d)))
                pods_with_ctr_pull.add(pn)

    for p in pods:
        pn = p.metadata.name or "?"
        if pn in pods_with_ctr_pull:
            continue
        cache = pod_event_cache.get(pn) or {}
        d = dur_s(cache.get("pod_pulling_first"), cache.get("pod_pulled_last"))
        if d is not None and d > 0.001:
            rows.append((f"{pn} (pod pull)", float(d)))

    rows.sort(key=lambda x: -x[1])
    return rows


def _merge_pie_slices(durs: List[Tuple[str, float]]) -> Tuple[List[Tuple[str, float]], float]:
    total = sum(d for _, d in durs)
    merged: List[Tuple[str, float]] = []
    other = 0.0
    for s, d in durs:
        if d / total < 0.03:
            other += d
        else:
            merged.append((s, d))
    if other > 0:
        merged.append(("Other", other))
    total_m = sum(d for _, d in merged)
    return merged, total_m


def render_share_bars_table(
    raw_slices: List[Tuple[str, float]],
    title: str,
    empty_msg: str,
    max_rows: int = 14,
    bar_width: int = 36,
) -> Table:
    """Horizontal bar chart (readable in narrow terminals; no round pie)."""
    durs = [(s, d) for s, d in raw_slices if d > 0.001]
    tbl_kw: Dict[str, Any] = {
        "show_header": True,
        "header_style": "bold",
        "box": None,
        "expand": True,
        "title": title,
    }
    t = Table(**tbl_kw)
    t.add_column("Step", overflow="ignore", min_width=16)
    t.add_column("%", justify="right", width=7, overflow="ignore")
    t.add_column("Bar", overflow="ignore", min_width=bar_width + 2)
    t.add_column("sec", justify="right", width=8, overflow="ignore")
    if not durs:
        t.add_row(Text(empty_msg, style="dim"), "-", "", "-")
        return t
    merged, total = _merge_pie_slices(durs)
    for i, (s, d) in enumerate(merged[:max_rows]):
        pct = (d / total) * 100 if total > 0 else 0.0
        filled = int(round(pct / 100.0 * bar_width))
        filled = max(0, min(filled, bar_width))
        bar_plain = "█" * filled + "·" * (bar_width - filled)
        color = PIE_PALETTE[i % len(PIE_PALETTE)]
        bar_cell = Text(bar_plain, style=Style(color=color))
        label = Text(str(s), style=Style(color=color))
        t.add_row(label, f"{pct:.1f}", bar_cell, f"{d:.0f}")
    if len(merged) > max_rows:
        t.add_row(Text("…", style="dim"), "", "", "")
    return t


def render_share_bars_block(
    phase_tracker: List[Tuple[str, datetime]],
    pods: List[client.V1Pod],
    pod_event_cache: Dict[str, Dict[str, Any]],
    started_at: datetime,
    *,
    max_rows: int = 10,
    bar_width: int = 28,
) -> Table:
    """Phase timeline + per-Pod workload + per-Container image pull (three separate 100% domains)."""
    outer = Table(box=None, show_header=False, expand=True)
    outer.add_column("share", overflow="ignore")
    outer.add_row(
        render_share_bars_table(
            phase_durations_seconds(phase_tracker),
            "Phase time (AppManager states)",
            "no phase data yet",
            max_rows=max_rows,
            bar_width=bar_width,
        )
    )
    outer.add_row(
        Text("100% = phase durations on one install timeline (not comparable to pod/container blocks).", style="dim")
    )
    outer.add_row(
        render_share_bars_table(
            build_per_pod_track_slices(pods, pod_event_cache, started_at),
            "By Pod (sched + pull + Started->Ready per pod)",
            "no pod timing yet",
            max_rows=max_rows,
            bar_width=bar_width,
        )
    )
    outer.add_row(
        Text(
            "100% = sum over pods of (sched+pull+S->R); multi-pod apps show which Pod name dominates.",
            style="dim",
        )
    )
    outer.add_row(
        render_share_bars_table(
            build_per_container_pull_slices(pods, pod_event_cache),
            "By Container — image pull (Pulling->Pulled)",
            "no per-container pull events yet",
            max_rows=max_rows,
            bar_width=bar_width,
        )
    )
    outer.add_row(
        Text(
            "100% = sum of measured pull windows; row = pod/container. Not additive with phase % or pod block.",
            style="dim",
        )
    )
    return outer


def render_share_legend_table(
    raw_slices: List[Tuple[str, float]],
    title: Optional[str],
    empty_msg: str,
    max_rows: int = 12,
) -> Table:
    """Percentage table only (no ASCII pie) — minimal vertical space."""
    durs = [(s, d) for s, d in raw_slices if d > 0.001]
    tbl_kw: Dict[str, Any] = {"show_header": True, "header_style": "bold", "box": None, "expand": True}
    if title:
        tbl_kw["title"] = title
    t = Table(**tbl_kw)
    t.add_column("Step", overflow="ignore", min_width=14)
    t.add_column("%", justify="right", width=7, overflow="ignore")
    t.add_column("s", justify="right", width=8, overflow="ignore")
    if not durs:
        t.add_row(Text(empty_msg, style="dim"), "-", "-")
        return t
    merged, total = _merge_pie_slices(durs)
    for i, (s, d) in enumerate(merged[:max_rows]):
        color = PIE_PALETTE[i % len(PIE_PALETTE)]
        pct = (d / total) * 100 if total > 0 else 0.0
        t.add_row(Text(s, style=Style(color=color)), f"{pct:.1f}", f"{d:.0f}")
    if len(merged) > max_rows:
        t.add_row(Text("…", style="dim"), "", "")
    return t


def render_pie_chart(
    raw_slices: List[Tuple[str, float]],
    legend_first_col: str,
    empty_msg: str,
    footnote: Optional[str] = None,
    radius: int = 4,
    max_legend_rows: int = 16,
) -> Table:
    """
    Terminal pie chart from (label, seconds) slices.
    """
    import math

    durs = [(s, d) for s, d in raw_slices if d > 0.001]
    if not durs:
        t = Table(box=None)
        t.add_column("Share")
        t.add_row(Text(empty_msg, style="dim"))
        return t

    merged, total = _merge_pie_slices(durs)

    slices_angles: List[Tuple[str, float, float, str]] = []
    acc = 0.0
    for i, (s, d) in enumerate(merged):
        frac = d / total if total > 0 else 0.0
        start = acc * 2 * math.pi
        acc += frac
        end = acc * 2 * math.pi
        color = PIE_PALETTE[i % len(PIE_PALETTE)]
        slices_angles.append((s, start, end, color))

    r = max(3, min(radius, 10))
    y_scale = 0.5
    pie = Text()
    for y in range(-r, r + 1):
        for x in range(-r, r + 1):
            dx = x
            dy = y * y_scale
            dist = (dx * dx + dy * dy) ** 0.5
            if dist > r:
                pie.append(" ")
                continue
            ang = math.atan2(dy, dx)
            if ang < 0:
                ang += 2 * math.pi
            chosen = None
            for _s, a0, a1, color in slices_angles:
                if a0 <= ang < a1 or (a1 >= 2 * math.pi and ang < (a1 - 2 * math.pi)):
                    chosen = (_s, color)
                    break
            if not chosen:
                chosen = (slices_angles[-1][0], slices_angles[-1][3])
            _, color = chosen
            pie.append("█", style=Style(color=color))
        pie.append("\n")

    legend = Table(show_header=True, header_style="bold", box=None, expand=True)
    legend.add_column(legend_first_col, overflow="ignore", min_width=8)
    legend.add_column("%", justify="right", width=6, overflow="ignore")
    legend.add_column("s", justify="right", width=7, overflow="ignore")
    for i, (s, d) in enumerate(merged[:max_legend_rows]):
        color = PIE_PALETTE[i % len(PIE_PALETTE)]
        pct = (d / total) * 100 if total > 0 else 0.0
        label = Text(s, style=Style(color=color))
        legend.add_row(label, f"{pct:.1f}", f"{d:.0f}")
    if len(merged) > max_legend_rows:
        legend.add_row(Text("…", style="dim"), "", "")

    inner = Table(box=None)
    inner.add_column("Pie")
    inner.add_column("Legend")
    inner.add_row(pie, legend)

    if not footnote:
        return inner

    outer = Table(box=None, show_header=False)
    outer.add_column("Block")
    outer.add_row(inner)
    outer.add_row(Text(footnote, style="dim"))
    return outer


def render_phase_pie(
    tracker: List[Tuple[str, datetime]],
    radius: int = 4,
    max_legend_rows: int = 16,
) -> Table:
    return render_pie_chart(
        phase_durations_seconds(tracker),
        "Phase",
        "no phase durations yet",
        footnote=None,
        radius=radius,
        max_legend_rows=max_legend_rows,
    )


def render_pod_track_pie(
    pods: List[client.V1Pod],
    pod_event_cache: Dict[str, Dict[str, Any]],
    started_at: datetime,
    radius: int = 4,
    max_legend_rows: int = 16,
    footnote: Optional[str] = (
        "Per Pod: sched+pull+Started->Ready summed for that pod; % = share across pods."
    ),
) -> Table:
    slices = build_per_pod_track_slices(pods, pod_event_cache, started_at)
    return render_pie_chart(
        slices,
        "Pod",
        "no pod timing yet",
        footnote=footnote,
        radius=radius,
        max_legend_rows=max_legend_rows,
    )


def render_container_pull_pie(
    pods: List[client.V1Pod],
    pod_event_cache: Dict[str, Dict[str, Any]],
    radius: int = 4,
    max_legend_rows: int = 16,
    footnote: Optional[str] = (
        "Per container: Pulling->Pulled from events; % = share across rows; (pod pull) = events not split by container."
    ),
) -> Table:
    slices = build_per_container_pull_slices(pods, pod_event_cache)
    return render_pie_chart(
        slices,
        "pod/ctr",
        "no container pull data yet",
        footnote=footnote,
        radius=radius,
        max_legend_rows=max_legend_rows,
    )


def render_share_compact_row(
    phase_tracker: List[Tuple[str, datetime]],
    pods: List[client.V1Pod],
    pod_event_cache: Dict[str, Dict[str, Any]],
    started_at: datetime,
) -> Table:
    """One row: phase | per-pod track; container pull pie needs --pie full or see bars."""
    row = Table(box=None, expand=True)
    row.add_column("Phase", overflow="ignore")
    row.add_column("By Pod", overflow="ignore")
    row.add_row(
        render_phase_pie(phase_tracker, radius=4, max_legend_rows=8),
        render_pod_track_pie(
            pods,
            pod_event_cache,
            started_at,
            radius=4,
            max_legend_rows=8,
            footnote=None,
        ),
    )
    wrap = Table(box=None, show_header=False, expand=True)
    wrap.add_column("block", overflow="ignore")
    wrap.add_row(row)
    wrap.add_row(
        Text(
            "L: phase %. R: which Pod (sched+pull+S->R) is largest. Container pull %: --pie full or default bars.",
            style="dim",
        )
    )
    return wrap


def render_share_off_row(
    phase_tracker: List[Tuple[str, datetime]],
    pods: List[client.V1Pod],
    pod_event_cache: Dict[str, Dict[str, Any]],
    started_at: datetime,
) -> Table:
    """Tables only, stacked full width (avoids narrow columns / ellipsis)."""
    wrap = Table(box=None, show_header=False, expand=True)
    wrap.add_column("block", overflow="ignore")
    wrap.add_row(
        render_share_legend_table(
            phase_durations_seconds(phase_tracker),
            "Phase %",
            "no phase data",
            max_rows=12,
        )
    )
    wrap.add_row(
        render_share_legend_table(
            build_per_pod_track_slices(pods, pod_event_cache, started_at),
            "By Pod %",
            "no pod timing",
            max_rows=12,
        )
    )
    wrap.add_row(
        render_share_legend_table(
            build_per_container_pull_slices(pods, pod_event_cache),
            "By Ctr pull %",
            "no container pull data",
            max_rows=12,
        )
    )
    wrap.add_row(
        Text("Three blocks: phase | per-pod track | per-container pull (each own 100%).", style="dim")
    )
    return wrap


def render_container_table(pods: List[client.V1Pod]) -> Table:
    t = Table(show_header=True, header_style="bold", box=None, expand=True, pad_edge=False)
    t.add_column("Pod/Ctr", overflow="ignore", min_width=16, max_width=34)
    t.add_column("St", overflow="ignore", width=8)
    t.add_column("Rd", overflow="ignore", width=3)
    t.add_column("Rst", justify="right", width=4, overflow="ignore")
    t.add_column("Pl+", justify="right", width=7, overflow="ignore")
    t.add_column("Cre", overflow="ignore", min_width=16, max_width=20)
    t.add_column("SEv", overflow="ignore", min_width=16, max_width=20)
    t.add_column("SAt", overflow="ignore", min_width=16, max_width=20)
    t.add_column("Wait", overflow="ignore", min_width=10, max_width=18)
    t.add_column("Image", overflow="ignore", min_width=12, max_width=40)

    rows: List[Tuple[int, str, List[str]]] = []
    for p in pods:
        if not p.status:
            continue

        # Best-effort per-container event times (requires events embedded in pod annotations cache upstream).
        # Caller may attach a precomputed map on pod object dynamically; otherwise blank.
        per_ct: Dict[str, Dict[str, Optional[datetime]]] = getattr(p, "_per_container_event_times", {})  # type: ignore[attr-defined]

        cstat = p.status.container_statuses
        if not cstat:
            phase = (p.status.phase or "-") or "-"
            rows.append(
                (
                    0,
                    p.metadata.name or "-",
                    [
                        f"{p.metadata.name or '-'}/—",
                        phase,
                        "?",
                        "0",
                        "-",
                        "-",
                        "-",
                        "-",
                        "no containerStatuses yet (scheduling/pulling?)",
                        "-",
                    ],
                )
            )
            continue

        for cs in cstat:
            state = "-"
            started_at: Optional[datetime] = None
            waiting_reason = ""
            if cs.state:
                if cs.state.running and cs.state.running.started_at:
                    state = "running"
                    started_at = cs.state.running.started_at
                elif cs.state.waiting:
                    state = "waiting"
                    waiting_reason = cs.state.waiting.reason or ""
                elif cs.state.terminated:
                    state = "terminated"
                    waiting_reason = cs.state.terminated.reason or ""

            ce = per_ct.get(cs.name) or {}
            pulling_first = ce.get("pulling_first")
            pulled_last = ce.get("pulled_last")
            created_first = ce.get("created_first")
            started_first = ce.get("started_first")
            pull_d = fmt_dur(dur_s(pulling_first, pulled_last))

            key = f"{p.metadata.name}/{cs.name}"
            row = [
                key,
                state,
                "Y" if cs.ready else "N",
                str(int(cs.restart_count or 0)),
                pull_d,
                dt_to_iso(created_first),
                dt_to_iso(started_first),
                dt_to_iso(started_at),
                waiting_reason or "-",
                cs.image or "-",
            ]
            # Sort: waiting first, then not ready, then stable
            prio = 0
            if state == "waiting":
                prio = 0
            elif not cs.ready:
                prio = 1
            else:
                prio = 2
            rows.append((prio, key, row))

    for _, _, r in sorted(rows, key=lambda x: (x[0], x[1]))[:60]:
        t.add_row(*r)
    return t


def summarize_pod_containers(pod: client.V1Pod) -> Tuple[int, int, int, Optional[datetime], Optional[str]]:
    running_ct = 0
    total_ct = 0
    waiting_ct = 0
    latest_started_at: Optional[datetime] = None
    worst_wait_reason: Optional[str] = None
    if pod.status and pod.status.container_statuses:
        total_ct = len(pod.status.container_statuses)
        for cs in pod.status.container_statuses:
            if cs.state and cs.state.running and cs.state.running.started_at:
                running_ct += 1
                if latest_started_at is None or cs.state.running.started_at > latest_started_at:
                    latest_started_at = cs.state.running.started_at
            if cs.state and cs.state.waiting:
                r = cs.state.waiting.reason
                if r in ("ContainerCreating", "ImagePullBackOff", "ErrImagePull", "PodInitializing", "CrashLoopBackOff"):
                    waiting_ct += 1
                    if not worst_wait_reason:
                        worst_wait_reason = r
    return running_ct, total_ct, waiting_ct, latest_started_at, worst_wait_reason


def render(
    appmgr_name: str,
    namespace: str,
    am_state: str,
    am_progress: str,
    am_msg: str,
    am_time: Optional[datetime],
    started_at: datetime,
    pods: List[client.V1Pod],
    pod_event_cache: Dict[str, Dict[str, Any]],
    phase_tracker: List[Tuple[str, datetime]],
    pie_mode: str = "bars",
    *,
    share_max_rows: int = 10,
    pod_filter_hint: str = "",
    share_bar_width: int = 28,
) -> Table:
    root = Table(
        title=f"Install Live Monitor  appmgr={appmgr_name}  ns={namespace}",
        show_lines=False,
        expand=True,
    )
    root.add_column("Section", style="bold", width=12, overflow="ignore", no_wrap=True)
    root.add_column("Details", overflow="ignore", no_wrap=False)

    elapsed = dur_s(started_at, now_utc())
    am_line = Text()
    am_line.append(f"state={am_state}  progress={am_progress}  elapsed={fmt_dur(elapsed)}\n")
    am_line.append(f"statusTime={dt_to_iso(am_time)}\n")
    am_line.append(f"message={am_msg}")
    root.add_row("ApplicationManager", am_line)
    root.add_row("Phases", render_phase_table(phase_tracker))
    if pie_mode == "off":
        root.add_row("Share", render_share_off_row(phase_tracker, pods, pod_event_cache, started_at))
    elif pie_mode == "full":
        root.add_row("Phase share", render_phase_pie(phase_tracker, radius=8, max_legend_rows=14))
        root.add_row(
            "Pod share",
            render_pod_track_pie(
                pods,
                pod_event_cache,
                started_at,
                radius=8,
                max_legend_rows=14,
            ),
        )
        root.add_row(
            "Ctr pull",
            render_container_pull_pie(
                pods,
                pod_event_cache,
                radius=8,
                max_legend_rows=14,
            ),
        )
    elif pie_mode == "compact":
        root.add_row("Share", render_share_compact_row(phase_tracker, pods, pod_event_cache, started_at))
    else:
        root.add_row(
            "Share",
            render_share_bars_block(
                phase_tracker,
                pods,
                pod_event_cache,
                started_at,
                max_rows=share_max_rows,
                bar_width=share_bar_width,
            ),
        )

    pods_tbl = Table(show_header=True, header_style="bold", box=None, expand=True, pad_edge=False)
    pods_tbl.add_column("Pod", overflow="ignore", min_width=14, max_width=36)
    pods_tbl.add_column("Node", overflow="ignore", min_width=6, max_width=14)
    pods_tbl.add_column("Ph", overflow="ignore", width=7)
    pods_tbl.add_column("Sch+", justify="right", overflow="ignore", width=7)
    pods_tbl.add_column("Pl+", justify="right", overflow="ignore", width=7)
    pods_tbl.add_column("S→R+", justify="right", overflow="ignore", width=7)
    pods_tbl.add_column("Warn", overflow="ignore", min_width=8, max_width=22)
    pods_tbl.add_column("Msg", overflow="ignore", min_width=10, max_width=32)
    pods_tbl.add_column("Ctr", overflow="ignore", min_width=10, max_width=28)

    for p in sorted(pods, key=lambda x: (x.metadata.creation_timestamp or started_at)):
        created = p.metadata.creation_timestamp
        scheduled = pod_time_from_condition(p, "PodScheduled", "True")
        ready = pod_time_from_condition(p, "Ready", "True")
        sched_d = fmt_dur(dur_s(created, scheduled))

        pod_name = p.metadata.name
        cache = pod_event_cache.get(pod_name) or {}
        pulling_first = cache.get("pod_pulling_first")
        pulled_last = cache.get("pod_pulled_last")
        started_ev = cache.get("pod_started_first")
        warn = cache.get("warn") or {}
        warn_reason = warn.get("latest_reason")
        warn_time: Optional[datetime] = warn.get("latest_time")
        warn_first_by_reason: Dict[str, datetime] = warn.get("first_by_reason") or {}
        warn_msg = warn.get("latest_message") or ""

        pull_d = fmt_dur(dur_s(pulling_first, pulled_last))
        start_ready_d = fmt_dur(dur_s(started_ev, ready))

        running_ct, total_ct, waiting_ct, latest_started_at, worst_wait_reason = summarize_pod_containers(p)
        c_line = f"running {running_ct}/{total_ct}"
        if waiting_ct > 0:
            c_line += f"  waiting={waiting_ct}"
            if worst_wait_reason:
                c_line += f"({worst_wait_reason})"
        if latest_started_at:
            c_line += f"  latestStartedAt={dt_to_iso(latest_started_at)}"

        warn_cell = "-"
        if warn_reason:
            first = warn_first_by_reason.get(warn_reason)
            since = fmt_dur(dur_s(first, now_utc())) if first else "-"
            age = fmt_dur(dur_s(warn_time, now_utc())) if warn_time else "-"
            warn_cell = f"{warn_reason} since={since} last={age}"

        warn_msg_cell = "-"
        if warn_msg:
            warn_msg_cell = str(warn_msg).replace("\n", " ").replace("\t", " ")
            if len(warn_msg_cell) > 500:
                warn_msg_cell = warn_msg_cell[:497] + "..."

        pods_tbl.add_row(
            p.metadata.name,
            p.spec.node_name or "-",
            (p.status.phase if p.status else "-") or "-",
            sched_d,
            pull_d,
            start_ready_d,
            warn_cell,
            warn_msg_cell,
            c_line,
        )

    pods_panel: Any = pods_tbl
    if pod_filter_hint:
        pods_panel = Group(Text(pod_filter_hint, style="dim"), pods_tbl)
    root.add_row("Pods", pods_panel)
    root.add_row("Containers", render_container_table(pods))
    return root


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--namespace", help="Kubernetes namespace (optional, can be auto-detected from ApplicationManager)")
    ap.add_argument("--appmgr", help="ApplicationManager name (cluster-scoped). If omitted, use --app to arm before install")
    ap.add_argument("--app", help="App name (arming mode: start before install and wait for appmgr)")
    ap.add_argument("--owner", help="Owner username (optional, improves pod filtering)")
    ap.add_argument("--refresh", type=float, default=1.0, help="Refresh interval seconds (default 1.0)")
    ap.add_argument("--until-running", action="store_true", help="Exit when appmgr state becomes Running")
    ap.add_argument(
        "--wait-new-install",
        action="store_true",
        help="With --app only: do not attach to an existing ApplicationManager; wait for an install whose op time is after this process started",
    )
    ap.add_argument(
        "--pie",
        choices=("bars", "compact", "full", "off"),
        default="bars",
        help="Share viz: bars=horizontal bars full width (default); compact=two small pies; full=large pies; off=tables only",
    )
    ap.add_argument(
        "--share-max-rows",
        type=int,
        default=10,
        metavar="N",
        help="Max bar-chart rows per table (default 10); lower if the Live UI is vertically cropped",
    )
    ap.add_argument(
        "--share-bar-width",
        type=int,
        default=28,
        metavar="N",
        help="Horizontal bar width in characters for --pie bars (default 28)",
    )
    ap.add_argument(
        "--live-overflow",
        choices=("crop", "ellipsis", "visible"),
        default="crop",
        help="Rich Live when output exceeds terminal height: crop=show top lines only (default); ellipsis=last line '...'; visible=full height (may not clear cleanly while refreshing)",
    )
    ap.add_argument(
        "--no-alt-screen",
        action="store_true",
        help="Do not use the terminal alternate screen (default: use it on a real TTY so Live gets a full-height buffer from row 1)",
    )
    args = ap.parse_args()

    load_kube_config()
    co = client.CustomObjectsApi()
    v1 = client.CoreV1Api()
    use_ft = sys.stdout.isatty()
    if os.environ.get("FORCE_TERMINAL_UI", "").lower() in ("1", "true", "yes"):
        use_ft = True
    if not sys.stdout.isatty():
        print(
            "Note: stdout is not a TTY; if the UI is blank, try: export FORCE_TERMINAL_UI=1",
            file=sys.stderr,
        )
    console = Console(force_terminal=use_ft)

    try:
        arm_start = now_utc()
        appmgr_name, namespace, detected_owner = pick_appmgr_or_wait(
            co=co,
            appmgr=args.appmgr,
            app=args.app,
            owner=args.owner,
            namespace=args.namespace,
            arm_start=arm_start,
            refresh=args.refresh,
            wait_new_install=args.wait_new_install,
            filter_namespace=args.namespace,
        )
        if not args.owner and detected_owner:
            args.owner = detected_owner
    except Exception as e:
        console.print(f"[red]ERROR[/red] {e}")
        return 2

    started_at = now_utc()
    # pod_event_cache[podName] = {
    #   "ts": float,
    #   "pod_pulling_first": datetime|None,
    #   "pod_pulled_last": datetime|None,
    #   "pod_started_first": datetime|None,
    #   "per_container": {containerName: {...times...}}
    # }
    pod_event_cache: Dict[str, Dict[str, Any]] = {}
    phase_tracker: List[Tuple[str, datetime]] = []

    use_alt_screen = bool(console.is_terminal and not args.no_alt_screen)
    if use_alt_screen:
        console.clear()

    with Live(
        console=console,
        refresh_per_second=max(1, int(1 / max(0.1, args.refresh))),
        vertical_overflow=args.live_overflow,
        screen=use_alt_screen,
    ) as live:
        while True:
            try:
                am: Dict[str, Any] = {}
                try:
                    am = get_custom_object(co, appmgr_name)
                    am_state, am_progress, am_msg, am_time = summarize_appmgr(am)
                except Exception as e:
                    am_state, am_progress, am_msg, am_time = "?", "-", f"get appmgr failed: {e}", None
                update_phase_tracker(phase_tracker, am_state, am_time)

                # Prefer namespace from appmgr spec once available
                try:
                    spec_ns = str(((am.get("spec") or {}).get("appNamespace")) or "")
                    if spec_ns:
                        namespace = spec_ns
                except Exception:
                    pass

                raw_pods = list_pods(v1, namespace)
                pods, pod_hint = pick_pods_for_monitor(raw_pods, args.app, args.owner)

                # Update per-pod event cache (5s TTL) for Pulling/Pulled/Started timings
                now = time.time()
                for p in pods:
                    pn = p.metadata.name
                    c = pod_event_cache.get(pn)
                    if c and (now - float(c.get("ts") or 0.0)) < 5.0:
                        continue
                    evs = list_events_for_pod(v1, namespace, pn)
                    pulling_first = find_first_event_time(evs, "Pulling")
                    pulled_last = find_last_event_time(evs, "Pulled")
                    started_ev = find_first_event_time(evs, "Started")
                    per_container = build_container_event_times(evs, p)
                    warn = summarize_warning(evs)

                    # Attach per-container to pod for container table rendering (best-effort).
                    setattr(p, "_per_container_event_times", per_container)

                    pod_event_cache[pn] = {
                        "ts": now,
                        "pod_pulling_first": pulling_first,
                        "pod_pulled_last": pulled_last,
                        "pod_started_first": started_ev,
                        "per_container": per_container,
                        "warn": warn,
                    }

                live.update(
                    render(
                        appmgr_name,
                        namespace,
                        am_state,
                        am_progress,
                        am_msg,
                        am_time,
                        started_at,
                        pods,
                        pod_event_cache,
                        phase_tracker,
                        pie_mode=args.pie,
                        share_max_rows=max(4, min(30, args.share_max_rows)),
                        share_bar_width=max(12, min(60, args.share_bar_width)),
                        pod_filter_hint=pod_hint,
                    )
                )

                if args.until_running and am_state.lower() == "running":
                    break

            except Exception as e:
                print(f"[install-speed] monitor loop error: {e}", file=sys.stderr)
                traceback.print_exc()

            time.sleep(args.refresh)

    console.print("[green]DONE[/green] appmgr reached Running" if args.until_running else "DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

