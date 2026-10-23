"""Sends an ntfy message when Trivy Operator finds a new CRITICAL CVE (with a fix) in a running image.

Reads every VulnerabilityReport (system/trivy-operator rescans each image daily). The first run
with reports sends one baseline summary; later runs send only CVEs not seen before. Fixed CVEs
leave the state, so one that comes back is sent again. TRIVY_NOTIFY=false only logs.
"""
import json
import os
import ssl
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

STATE_PATH = "/state/trivy.json"
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
REPORTS = "/apis/aquasecurity.github.io/v1alpha1/vulnerabilityreports"
MAX_TEXT = 3500


def image_of(report):
    r = report.get("report", {})
    art, reg = r.get("artifact", {}), r.get("registry", {}).get("server", "")
    name = f"{reg}/{art.get('repository', '?')}" if reg else art.get("repository", "?")
    return f"{name}:{art['tag']}" if art.get("tag") else name


def workload_of(report):
    labels = report.get("metadata", {}).get("labels", {})
    return (f"{labels.get('trivy-operator.resource.namespace', '?')}/"
            f"{labels.get('trivy-operator.resource.name', '?')}")


def criticals(reports):
    """{key: finding} for every CRITICAL vulnerability that has a fixed version."""
    found = {}
    for rep in reports:
        image, workload = image_of(rep), workload_of(rep)
        for v in rep.get("report", {}).get("vulnerabilities") or []:
            if v.get("severity") != "CRITICAL" or not v.get("fixedVersion"):
                continue
            key = f"{image}|{v['vulnerabilityID']}|{v.get('resource', '')}"
            found[key] = {"image": image, "workload": workload, "id": v["vulnerabilityID"],
                          "pkg": v.get("resource", ""), "installed": v.get("installedVersion", ""),
                          "fixed": v["fixedVersion"]}
    return found


def by_image(findings):
    groups = defaultdict(list)
    for f in findings:
        groups[(f["image"], f["workload"])].append(f)
    return sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))


def clip(lines):
    text = ""
    for line in lines:
        if len(text) + len(line) + 1 > MAX_TEXT:
            return text + "…"
        text += line + "\n"
    return text.rstrip("\n")


def new_message(findings):
    lines = []
    for (image, workload), fs in by_image(findings):
        lines.append(f"{workload} ({image})")
        lines += [f"  {f['id']} {f['pkg']} {f['installed']} -> {f['fixed']}" for f in fs]
    n = len(findings)
    return f"Trivy: {n} new critical CVE{'s' if n > 1 else ''}", clip(lines)


def baseline_message(findings):
    groups = by_image(findings)
    lines = [f"{len(fs)}  {workload} ({image})" for (image, workload), fs in groups]
    return (f"Trivy baseline: {len(findings)} critical CVEs (with a fix) in {len(groups)} images",
            clip(lines) or "Nothing critical.")


def decide(current, state, today):
    """(message or None, new state). state is None on the first run."""
    if state is None:
        return baseline_message(list(current.values())), {k: today for k in current}
    new = [current[k] for k in sorted(current) if k not in state]
    kept = {k: state.get(k, today) for k in current}
    return (new_message(new) if new else None), kept


def load_state(path):
    try:
        with open(path) as f:
            state = json.load(f)
        return state if isinstance(state, dict) else None
    except (OSError, ValueError):
        return None


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def list_reports():
    with open(f"{SA}/token") as f:
        token = f.read().strip()
    ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    req = urllib.request.Request(f"https://kubernetes.default.svc{REPORTS}",
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
        return json.load(resp).get("items", [])


def ntfy_sender(title, text, priority, tags):
    req = urllib.request.Request(
        f"https://ntfy.sh/{os.environ['NTFY_TOPIC']}", data=text.encode(), method="POST",
        headers={"Authorization": f"Bearer {os.environ['NTFY_TOKEN']}", "Title": title,
                 "Priority": str(priority), "Tags": tags})
    urllib.request.urlopen(req, timeout=20).close()


def main(reports=None, sender=None, state_path=STATE_PATH, today=None, notify=None):
    reports = list_reports() if reports is None else reports
    sender = sender or ntfy_sender
    today = today or datetime.now(timezone.utc).date().isoformat()
    if notify is None:
        notify = os.environ.get("TRIVY_NOTIFY", "true").lower() == "true"
    if not reports:  # operator not installed or first scans not done: no empty baseline
        print("no vulnerability reports yet")
        return 0
    current = criticals(reports)
    state = load_state(state_path)
    msg, new_state = decide(current, state, today)
    print(f"reports={len(reports)} critical={len(current)} first_run={state is None} "
          f"send={msg is not None}")
    if msg:
        if notify:
            try:
                sender(msg[0], msg[1], 4 if state is not None else 3, "rotating_light")
            except Exception as e:  # retried at the next run
                print(f"send failed: {type(e).__name__}", file=sys.stderr)
                return 1
        else:
            print(f"would send: {msg[0]}\n{msg[1]}")
    save_state(state_path, new_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
