from flask import Flask, request, jsonify
import json, os, requests, datetime, pytz

app = Flask(__name__)

# ── Credentials ──────────────────────────────────────────────────────────────
FUB_API_KEY     = "fka_02ahjD8BtIR6TigauJvyV3yu1KYD05QHmu"
FUB_BASE        = "https://api.followupboss.com/v1"
FUB_HEADERS     = {"X-System": "Donovan-Reyes-AI", "X-System-Key": "4657362a30438245ce0d32634fe77213"}
RESEND_API_KEY  = "re_gUUpKVvT_2a3iMC1ro8szo6HwVpHWNBqW"
FROM_EMAIL      = "Donovan Reyes <donovanreyes@your702agent.com>"
DONOVAN_EMAIL   = "donovanreyes@your702agent.com"
ADMIN_EMAIL     = "admin@greatlasvegasrealestate.com"

REALTOR_SOURCES = {"realtor.com", "realtor", "realtor.com agent", "realtor.com team"}

# ── Tier config (fallback if eligible-agents.json missing) ───────────────────
TIER_CONFIG = {
    "under_300k":  [15, 11],
    "t300k_500k":  [3, 34, 19, 17, 23, 36, 14, 16, 28, 20],
    "t500k_plus":  [2, 10, 9, 25, 33, 26, 27, 32]
}

RR_STATE_FILE      = "/data/rr-state.json"
ELIGIBLE_FILE      = "/data/eligible-agents.json"
HISTORY_FILE       = "/data/assignment-history.json"
STRIKE_FILE        = "/data/strike-log.json"

PT = pytz.timezone("America/Los_Angeles")

# ── Helpers ───────────────────────────────────────────────────────────────────
def fub_get(path, params=None):
    r = requests.get(f"{FUB_BASE}/{path}", auth=(FUB_API_KEY, ""),
                     headers=FUB_HEADERS, params=params, timeout=10)
    return r.json()

def fub_patch(path, data):
    r = requests.put(f"{FUB_BASE}/{path}", auth=(FUB_API_KEY, ""),
                     headers={**FUB_HEADERS, "Content-Type": "application/json"},
                     json=data, timeout=10)
    return r.json()

def fub_note(person_id, subject, body):
    requests.post(f"{FUB_BASE}/notes", auth=(FUB_API_KEY, ""),
                  headers={**FUB_HEADERS, "Content-Type": "application/json"},
                  json={"personId": person_id, "subject": subject, "body": body, "isHtml": True},
                  timeout=10)

def send_email(to, subject, html, cc=None):
    cc = cc or [ADMIN_EMAIL, DONOVAN_EMAIL]
    requests.post("https://api.resend.com/emails",
                  headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                  json={"from": FROM_EMAIL, "to": [to], "cc": cc, "subject": subject, "html": html},
                  timeout=10)

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return default

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def get_price_tier(person):
    price = person.get("price") or 0
    try:
        price = float(str(price).replace(",", "").replace("$", ""))
    except:
        price = 0
    if price <= 0:
        return "unknown"
    if price < 300000:
        return "under_300k"
    if price <= 500000:
        return "t300k_500k"
    return "t500k_plus"

def get_eligible_pool(tier):
    today = datetime.datetime.now(PT).strftime("%Y-%m-%d")
    eligible = load_json(ELIGIBLE_FILE, {})
    strike_log = load_json(STRIKE_FILE, {})

    # Check if eligible file is for today
    if eligible.get("date") != today:
        return None, "eligible-agents.json is outdated or missing"

    # Get paused agent IDs (second-strike notified today)
    paused_ids = set()
    for uid, entry in strike_log.items():
        for strike in entry.get("strikes", []):
            if strike.get("date") == today and strike.get("notified"):
                paused_ids.add(int(uid))

    if tier == "unknown":
        pool = []
        for t in ["under_300k", "t300k_500k", "t500k_plus"]:
            pool += [a for a in eligible.get(t, []) if a["userId"] not in paused_ids]
    else:
        pool = [a for a in eligible.get(tier, []) if a["userId"] not in paused_ids]

    return pool, None

def next_agent(tier, pool):
    rr = load_json(RR_STATE_FILE, {})
    state = rr.get(tier, {"index": 0})
    idx = state.get("index", 0) % len(pool)
    agent = pool[idx]
    rr[tier] = {"index": (idx + 1) % len(pool), "lastUpdated": datetime.datetime.now(PT).isoformat()}
    save_json(RR_STATE_FILE, rr)
    return agent

def log_assignment(person, agent):
    history = load_json(HISTORY_FILE, [])
    now = datetime.datetime.now(PT).isoformat()
    # Check if already in history
    for record in history:
        if record["personId"] == person["id"]:
            record["transfers"].append({
                "toUserId": agent["userId"], "toName": agent["name"],
                "transferredAt": now, "reason": "round-robin"
            })
            record["currentAssignee"] = {"userId": agent["userId"], "name": agent["name"], "assignedAt": now}
            save_json(HISTORY_FILE, history)
            return
    history.append({
        "personId": person["id"],
        "leadName": person.get("name"),
        "source": person.get("source"),
        "priceRange": get_price_tier(person),
        "createdAt": person.get("created"),
        "originalAssignee": {"userId": agent["userId"], "name": agent["name"], "email": agent["email"], "assignedAt": now},
        "transfers": [],
        "currentAssignee": {"userId": agent["userId"], "name": agent["name"], "assignedAt": now}
    })
    save_json(HISTORY_FILE, history)

# ── Main webhook handler ──────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.json or {}
    person = payload.get("person") or payload.get("data") or payload

    # Only process Realtor.com leads
    source = (person.get("source") or "").strip().lower()
    if source not in REALTOR_SOURCES:
        return jsonify({"status": "skipped", "reason": "not a Realtor.com lead"}), 200

    person_id = person.get("id")
    if not person_id:
        return jsonify({"status": "error", "reason": "no personId"}), 400

    # Fetch full person record from FUB
    full = fub_get(f"people/{person_id}")
    person = full if full.get("id") else person

    tier = get_price_tier(person)
    pool, err = get_eligible_pool(tier)

    if err or not pool:
        # Fallback: try all tiers
        pool, err2 = get_eligible_pool("unknown")
        if not pool:
            send_email(DONOVAN_EMAIL,
                f"[Unassigned Realtor.com Lead] {person.get('name')} — No eligible agents",
                f"<p>New Realtor.com lead <strong>{person.get('name')}</strong> could not be assigned — no eligible High-rated agents available. Please assign manually in FUB.</p>",
                cc=[ADMIN_EMAIL])
            return jsonify({"status": "unassigned", "reason": "no eligible agents"}), 200

    agent = next_agent(tier if pool else "unknown", pool)

    # Assign in FUB
    fub_patch(f"people/{person_id}", {"assignedUserId": agent["userId"]})

    # Enroll in Realtor.com Follow Up action plan (ID: 28)
    try:
        existing = requests.get(f"{FUB_BASE}/actionPlansPeople", auth=(FUB_API_KEY, ""),
                                headers=FUB_HEADERS, params={"personId": person_id}, timeout=10).json()
        active_plans = [p for p in existing.get("actionPlansPeople", []) if p.get("status", "").lower() == "running"]
        if not active_plans:
            requests.post(f"{FUB_BASE}/actionPlansPeople", auth=(FUB_API_KEY, ""),
                         headers={**FUB_HEADERS, "Content-Type": "application/json"},
                         json={"actionPlanId": 28, "personId": person_id}, timeout=10)
    except Exception as e:
        print(f"Action plan enrollment error: {e}")

    # Post FUB note
    fub_note(person_id, "Realtor.com Lead Assigned",
        f'<p><span data-user-id="{agent["userId"]}">{agent["name"]}</span>, '
        f'you have been assigned a new Realtor.com lead. You have <strong>1 minute</strong> '
        f'to make contact. Call or text now — speed wins this deal.</p>')

    # Send assignment email
    price = person.get("price") or "N/A"
    lead_name = person.get("name", "Unknown")
    phones = person.get("phones", [{}])
    phone = phones[0].get("value", "N/A") if phones else "N/A"
    emails = person.get("emails", [{}])
    email = emails[0].get("value", "N/A") if emails else "N/A"
    first = agent["name"].split()[0]

    html = f"""<html><body style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1a1a1a;max-width:720px">
<p>Hi {first},</p>
<p>You have been assigned a new Realtor.com lead. <strong>Contact them within 1 MINUTE.</strong></p>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;border-color:#ddd">
<tr style="background:#1F4E78;color:#fff"><th>Field</th><th>Detail</th></tr>
<tr><td>Lead Name</td><td><a href="https://app.followupboss.com/2/people/view/{person_id}">{lead_name}</a></td></tr>
<tr><td>Phone</td><td>{phone}</td></tr>
<tr><td>Email</td><td>{email}</td></tr>
<tr><td>Source</td><td>Realtor.com</td></tr>
<tr><td>Price Range</td><td>${price:,} ({tier.replace('_',' ')})</td></tr>
</table>
<br><p>⏱️ The clock is ticking. Speed to lead wins deals.</p>
<p>— Donovan Reyes Team</p>
</body></html>"""

    send_email(agent["email"],
        f"🏠 New Realtor.com Lead — {lead_name} — Contact Within 1 Minute", html)

    # Log assignment history
    log_assignment(person, agent)

    return jsonify({"status": "assigned", "agent": agent["name"], "lead": lead_name, "tier": tier}), 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": datetime.datetime.now(PT).isoformat()}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
