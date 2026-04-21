from datetime import datetime, date, timedelta
from flask import Flask, request, jsonify
import requests
import json
import os

app = Flask(__name__)

# --------- CẤU HÌNH GIST ----------
GIST_ID = os.environ.get("GIST_ID", "8a3b40053089341ad248e9f948e12237")
GIST_OWNER = os.environ.get("GIST_OWNER", "hgkhuyen255")
GIST_FILENAME = os.environ.get("GIST_FILENAME", "machines.json")

GIST_RAW_URL = f"https://gist.githubusercontent.com/{GIST_OWNER}/{GIST_ID}/raw/{GIST_FILENAME}"
GIST_API_URL = f"https://api.github.com/gists/{GIST_ID}"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")


def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def normalize_machine_id(machine_id: str) -> str:
    return (machine_id or "").strip().upper()


def normalize_app_name(app_name: str) -> str:
    return (app_name or "").strip()


def make_license_key(app_name: str, machine_id: str) -> str:
    app = normalize_app_name(app_name)
    mid = normalize_machine_id(machine_id)
    return f"{app}__{mid}"


def load_machines_from_gist() -> dict:
    try:
        r = requests.get(GIST_RAW_URL, timeout=10)
        r.raise_for_status()
        data = r.text.strip()
        if not data:
            return {}
        return json.loads(data)
    except Exception as e:
        print(f"[GIST] Lỗi đọc Gist: {e}")
        return {}


def save_machines_to_gist(machines: dict) -> bool:
    if not GITHUB_TOKEN:
        print("[GIST] Thiếu GITHUB_TOKEN, không thể update Gist.")
        return False

    content = json.dumps(machines, ensure_ascii=False, indent=2)
    payload = {
        "files": {
            GIST_FILENAME: {
                "content": content
            }
        }
    }
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
    }
    try:
        resp = requests.patch(GIST_API_URL, headers=headers, json=payload, timeout=15)
        resp.raise_for_status()
        print("[GIST] Đã cập nhật Gist thành công.")
        return True
    except Exception as e:
        print(f"[GIST] Lỗi cập nhật Gist: {e}")
        return False


def calc_remaining_days(expiry_str: str) -> int:
    expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
    today = date.today()
    return (expiry_date - today).days


def check_admin_secret(req) -> bool:
    header_secret = req.headers.get("X-Admin-Secret", "")
    query_secret = req.args.get("secret", "")
    return bool(ADMIN_SECRET) and (header_secret == ADMIN_SECRET or query_secret == ADMIN_SECRET)


def get_license_record(machines: dict, app_name: str, machine_id: str):
    license_key = make_license_key(app_name, machine_id)
    return license_key, machines.get(license_key)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "message": "running",
        "gist_owner": GIST_OWNER,
        "gist_id": GIST_ID,
        "gist_filename": GIST_FILENAME,
    }), 200


@app.route("/check_machine", methods=["POST"])
def check_machine():
    data = request.get_json(silent=True) or {}
    machine_id = normalize_machine_id(data.get("machine_id", ""))
    user_name = (data.get("user_name", "") or "").strip()
    app_name = normalize_app_name(data.get("app_name", ""))
    app_version = (data.get("app_version", "") or "").strip()
    now_utc = utc_now_iso()

    if not machine_id:
        return jsonify({"ok": False, "message": "Thiếu machine_id", "remaining_days": 0}), 400

    if not app_name:
        return jsonify({"ok": False, "message": "Thiếu app_name", "remaining_days": 0}), 400

    machines = load_machines_from_gist()
    license_key, lic = get_license_record(machines, app_name, machine_id)

    if not lic:
        machines[license_key] = {
            "license_key": license_key,
            "machine_id": machine_id,
            "app_name": app_name,
            "app_version": app_version,
            "user_name": user_name or "unknown",
            "status": "pending",
            "expires_at": None,
            "created_at": now_utc,
            "last_seen_at": now_utc,
            "note": f"Request from machine at {now_utc}"
        }
        save_machines_to_gist(machines)

        return jsonify({
            "ok": False,
            "message": "Máy chưa được active. Yêu cầu đã gửi lên admin.",
            "remaining_days": 0,
            "machine_id": machine_id,
            "app_name": app_name,
            "license_key": license_key,
            "status": "pending",
        }), 200

    lic["license_key"] = license_key
    lic["machine_id"] = machine_id
    lic["app_name"] = app_name
    lic["last_seen_at"] = now_utc
    if user_name:
        lic["user_name"] = user_name
    if app_version:
        lic["app_version"] = app_version

    machines[license_key] = lic
    save_machines_to_gist(machines)

    status = (lic.get("status") or "pending").lower()

    if status == "pending":
        return jsonify({
            "ok": False,
            "message": "Máy đang ở trạng thái chờ kích hoạt. Liên hệ admin.",
            "remaining_days": 0,
            "machine_id": machine_id,
            "app_name": app_name,
            "license_key": license_key,
            "status": status,
        }), 200

    if status != "active":
        return jsonify({
            "ok": False,
            "message": f"License không ở trạng thái active ({status})",
            "remaining_days": 0,
            "machine_id": machine_id,
            "app_name": app_name,
            "license_key": license_key,
            "status": status,
        }), 200

    expires_at = lic.get("expires_at")
    if not expires_at:
        return jsonify({
            "ok": False,
            "message": "Máy đã được active nhưng chưa có expires_at. Liên hệ admin.",
            "remaining_days": 0,
            "machine_id": machine_id,
            "app_name": app_name,
            "license_key": license_key,
            "status": status,
        }), 200

    try:
        remaining = calc_remaining_days(expires_at)
    except Exception:
        return jsonify({
            "ok": False,
            "message": "expires_at không đúng định dạng YYYY-MM-DD.",
            "remaining_days": 0,
            "machine_id": machine_id,
            "app_name": app_name,
            "license_key": license_key,
            "status": status,
        }), 200

    if remaining < 0:
        return jsonify({
            "ok": False,
            "message": "License đã hết hạn, cần gia hạn.",
            "remaining_days": 0,
            "machine_id": machine_id,
            "app_name": app_name,
            "license_key": license_key,
            "status": "expired",
        }), 200

    return jsonify({
        "ok": True,
        "message": "OK",
        "remaining_days": remaining,
        "machine_id": machine_id,
        "app_name": app_name,
        "license_key": license_key,
        "status": status,
        "expires_at": expires_at,
    }), 200


@app.route("/admin/list_machines", methods=["GET"])
def admin_list_machines():
    if not check_admin_secret(request):
        return jsonify({"ok": False, "message": "Unauthorized"}), 401

    machines = load_machines_from_gist()
    return jsonify({
        "ok": True,
        "count": len(machines),
        "machines": machines,
    }), 200


@app.route("/admin/activate_machine", methods=["POST"])
def admin_activate_machine():
    if not check_admin_secret(request):
        return jsonify({"ok": False, "message": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    machine_id = normalize_machine_id(data.get("machine_id", ""))
    app_name = normalize_app_name(data.get("app_name", ""))
    expires_at = (data.get("expires_at", "") or "").strip()
    user_name = (data.get("user_name", "") or "").strip()
    note = (data.get("note", "") or "").strip()
    now_utc = utc_now_iso()

    if not machine_id or not app_name or not expires_at:
        return jsonify({"ok": False, "message": "Thiếu machine_id, app_name hoặc expires_at"}), 400

    try:
        datetime.strptime(expires_at, "%Y-%m-%d")
    except Exception:
        return jsonify({"ok": False, "message": "expires_at phải đúng định dạng YYYY-MM-DD"}), 400

    machines = load_machines_from_gist()
    license_key, lic = get_license_record(machines, app_name, machine_id)
    if not lic:
        lic = {
            "license_key": license_key,
            "machine_id": machine_id,
            "app_name": app_name,
            "created_at": now_utc,
        }

    lic["status"] = "active"
    lic["expires_at"] = expires_at
    lic["last_seen_at"] = now_utc
    lic["license_key"] = license_key
    lic["machine_id"] = machine_id
    lic["app_name"] = app_name
    if user_name:
        lic["user_name"] = user_name
    if note:
        lic["note"] = note

    machines[license_key] = lic
    ok = save_machines_to_gist(machines)

    return jsonify({
        "ok": ok,
        "message": "Đã active" if ok else "Lưu Gist thất bại",
        "license_key": license_key,
        "machine_id": machine_id,
        "app_name": app_name,
        "expires_at": expires_at,
        "status": lic.get("status"),
    }), 200 if ok else 500


@app.route("/admin/set_status", methods=["POST"])
def admin_set_status():
    if not check_admin_secret(request):
        return jsonify({"ok": False, "message": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    machine_id = normalize_machine_id(data.get("machine_id", ""))
    app_name = normalize_app_name(data.get("app_name", ""))
    status = (data.get("status", "") or "").strip().lower()
    note = (data.get("note", "") or "").strip()
    now_utc = utc_now_iso()

    if not machine_id or not app_name or not status:
        return jsonify({"ok": False, "message": "Thiếu machine_id, app_name hoặc status"}), 400

    machines = load_machines_from_gist()
    license_key, lic = get_license_record(machines, app_name, machine_id)
    if not lic:
        return jsonify({"ok": False, "message": "Không tìm thấy license"}), 404

    lic["status"] = status
    lic["last_seen_at"] = now_utc
    if note:
        lic["note"] = note
    machines[license_key] = lic

    ok = save_machines_to_gist(machines)
    return jsonify({
        "ok": ok,
        "message": "Đã cập nhật status" if ok else "Lưu Gist thất bại",
        "license_key": license_key,
        "machine_id": machine_id,
        "app_name": app_name,
        "status": status,
    }), 200 if ok else 500


@app.route("/admin/extend_machine", methods=["POST"])
def admin_extend_machine():
    if not check_admin_secret(request):
        return jsonify({"ok": False, "message": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    machine_id = normalize_machine_id(data.get("machine_id", ""))
    app_name = normalize_app_name(data.get("app_name", ""))
    days = int(data.get("days", 0))
    note = (data.get("note", "") or "").strip()
    user_name = (data.get("user_name", "") or "").strip()

    if not machine_id or not app_name or days <= 0:
        return jsonify({"ok": False, "message": "Thiếu machine_id, app_name hoặc days không hợp lệ"}), 400

    machines = load_machines_from_gist()
    license_key, lic = get_license_record(machines, app_name, machine_id)
    if not lic:
        return jsonify({"ok": False, "message": "Không tìm thấy license"}), 404

    today = datetime.utcnow().date()
    old_expiry = lic.get("expires_at")

    if old_expiry:
        try:
            expiry_date = datetime.strptime(old_expiry, "%Y-%m-%d").date()
            if expiry_date < today:
                expiry_date = today
        except Exception:
            expiry_date = today
    else:
        expiry_date = today

    new_expiry = expiry_date + timedelta(days=days)

    lic["status"] = "active"
    lic["expires_at"] = new_expiry.strftime("%Y-%m-%d")
    lic["last_seen_at"] = utc_now_iso()
    if user_name:
        lic["user_name"] = user_name
    if note:
        lic["note"] = note

    machines[license_key] = lic
    ok = save_machines_to_gist(machines)

    return jsonify({
        "ok": ok,
        "message": "Đã gia hạn thành công" if ok else "Lưu Gist thất bại",
        "license_key": license_key,
        "machine_id": machine_id,
        "app_name": app_name,
        "days_added": days,
        "new_expires_at": lic["expires_at"],
        "status": lic.get("status"),
    }), 200 if ok else 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
