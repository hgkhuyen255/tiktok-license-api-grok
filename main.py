from datetime import datetime, date
from flask import Flask, request, jsonify
import requests
import json
import os


app = Flask(__name__)

# --------- CẤU HÌNH GIST ----------
# Set các biến này trong Cloud Run env nếu muốn đổi động
GIST_ID = os.environ.get("GIST_ID", "8a3b40053089341ad248e9f948e12237")
GIST_OWNER = os.environ.get("GIST_OWNER", "hgkhuyen255")
GIST_FILENAME = os.environ.get("GIST_FILENAME", "machines.json")

# URL RAW để đọc JSON
GIST_RAW_URL = f"https://gist.githubusercontent.com/{GIST_OWNER}/{GIST_ID}/raw/{GIST_FILENAME}"

# URL API để cập nhật Gist
GIST_API_URL = f"https://api.github.com/gists/{GIST_ID}"

# Secret chỉ dùng trên server
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")


def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


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


def ensure_admin(request_obj):
    secret = request_obj.headers.get("X-Admin-Secret", "")
    return bool(ADMIN_SECRET and secret == ADMIN_SECRET)


@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "ok": True,
        "service": "license-server",
        "endpoints": ["/health", "/check_machine", "/admin/activate_machine", "/admin/set_status", "/admin/list_machines"],
        "gist_owner": GIST_OWNER,
        "gist_id": GIST_ID,
        "gist_filename": GIST_FILENAME,
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "message": "running"})


@app.route("/check_machine", methods=["POST"])
def check_machine():
    data = request.get_json(silent=True) or {}
    machine_id = data.get("machine_id", "").strip().upper()
    user_name = data.get("user_name", "").strip()
    app_name = data.get("app_name", "").strip()
    app_version = data.get("app_version", "").strip()
    now_utc = utc_now_iso()

    if not machine_id:
        return jsonify({"ok": False, "message": "Thiếu machine_id", "remaining_days": 0}), 400

    machines = load_machines_from_gist()
    lic = machines.get(machine_id)

    if not lic:
        machines[machine_id] = {
            "user_name": user_name or "unknown",
            "status": "pending",
            "expires_at": None,
            "created_at": now_utc,
            "last_seen_at": now_utc,
            "app_name": app_name or None,
            "app_version": app_version or None,
            "note": f"Request from machine at {now_utc}",
        }
        save_machines_to_gist(machines)
        return jsonify({
            "ok": False,
            "message": "Máy chưa được active. Yêu cầu đã gửi lên admin.",
            "remaining_days": 0,
            "machine_id": machine_id,
            "status": "pending",
        }), 200

    lic["last_seen_at"] = now_utc
    if user_name:
        lic["user_name"] = user_name
    if app_name:
        lic["app_name"] = app_name
    if app_version:
        lic["app_version"] = app_version
    machines[machine_id] = lic
    save_machines_to_gist(machines)

    status = (lic.get("status") or "pending").lower()

    if status == "pending":
        return jsonify({
            "ok": False,
            "message": "Máy đang ở trạng thái chờ kích hoạt. Liên hệ admin.",
            "remaining_days": 0,
            "machine_id": machine_id,
            "status": status,
        }), 200

    if status != "active":
        return jsonify({
            "ok": False,
            "message": f"License không ở trạng thái active ({status})",
            "remaining_days": 0,
            "machine_id": machine_id,
            "status": status,
        }), 200

    expires_at = lic.get("expires_at")
    if not expires_at:
        return jsonify({
            "ok": False,
            "message": "Máy đã được active nhưng chưa có expires_at. Liên hệ admin.",
            "remaining_days": 0,
            "machine_id": machine_id,
            "status": status,
        }), 200

    try:
        remaining = calc_remaining_days(expires_at)
    except Exception:
        return jsonify({
            "ok": False,
            "message": "Định dạng expires_at không hợp lệ. Phải là YYYY-MM-DD.",
            "remaining_days": 0,
            "machine_id": machine_id,
            "status": status,
        }), 200

    if remaining < 0:
        return jsonify({
            "ok": False,
            "message": "License đã hết hạn, cần gia hạn.",
            "remaining_days": 0,
            "machine_id": machine_id,
            "status": "expired",
        }), 200

    return jsonify({
        "ok": True,
        "message": "OK",
        "remaining_days": remaining,
        "machine_id": machine_id,
        "status": status,
        "expires_at": expires_at,
    }), 200


@app.route("/admin/activate_machine", methods=["POST"])
def activate_machine():
    if not ensure_admin(request):
        return jsonify({"ok": False, "message": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    machine_id = data.get("machine_id", "").strip().upper()
    expires_at = data.get("expires_at", "").strip()
    user_name = data.get("user_name", "").strip()
    note = data.get("note", "").strip()

    if not machine_id or not expires_at:
        return jsonify({"ok": False, "message": "Thiếu machine_id hoặc expires_at"}), 400

    machines = load_machines_from_gist()
    lic = machines.get(machine_id, {})
    lic["status"] = "active"
    lic["expires_at"] = expires_at
    lic["last_seen_at"] = utc_now_iso()
    if not lic.get("created_at"):
        lic["created_at"] = utc_now_iso()
    if user_name:
        lic["user_name"] = user_name
    if note:
        lic["note"] = note
    machines[machine_id] = lic

    ok = save_machines_to_gist(machines)
    return jsonify({
        "ok": ok,
        "message": "Đã active" if ok else "Lưu Gist thất bại",
        "machine_id": machine_id,
        "expires_at": expires_at,
    })


@app.route("/admin/set_status", methods=["POST"])
def set_status():
    if not ensure_admin(request):
        return jsonify({"ok": False, "message": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    machine_id = data.get("machine_id", "").strip().upper()
    status = data.get("status", "").strip().lower()
    note = data.get("note", "").strip()

    if not machine_id or not status:
        return jsonify({"ok": False, "message": "Thiếu machine_id hoặc status"}), 400

    allowed = {"pending", "active", "blocked", "banned", "expired"}
    if status not in allowed:
        return jsonify({"ok": False, "message": f"status không hợp lệ. Chỉ nhận: {sorted(allowed)}"}), 400

    machines = load_machines_from_gist()
    lic = machines.get(machine_id)
    if not lic:
        return jsonify({"ok": False, "message": "Không tìm thấy machine_id"}), 404

    lic["status"] = status
    lic["last_seen_at"] = utc_now_iso()
    if note:
        lic["note"] = note
    machines[machine_id] = lic

    ok = save_machines_to_gist(machines)
    return jsonify({"ok": ok, "message": "Đã cập nhật status" if ok else "Lưu Gist thất bại", "machine_id": machine_id, "status": status})


@app.route("/admin/list_machines", methods=["GET"])
def list_machines():
    if not ensure_admin(request):
        return jsonify({"ok": False, "message": "Unauthorized"}), 401

    machines = load_machines_from_gist()
    return jsonify({
        "ok": True,
        "count": len(machines),
        "machines": machines,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
