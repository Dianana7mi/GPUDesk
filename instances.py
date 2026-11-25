"""Monitor the existing standard-container instance by its AutoDL UUID."""

from datetime import datetime, timezone
from threading import Event
from time import monotonic

import requests

from gpuhawk_core import ApiError, AuthError, positive_int, stock_rejected


STATUS_NAMES = {
    "shutdown": "已关机", "starting": "开机中", "running": "运行中",
    "shutting_down": "关机中", "created": "已创建", "created_clone_success": "克隆完成",
    "oss_merged": "迁移完成", "shutdown_by_starting_error": "开机异常后关机",
    "shutdown_by_running_error": "运行异常后关机",
    "create_failed": "创建失败",
}
STOPPED = {
    "shutdown", "created", "created_clone_success", "oss_merged",
    "shutdown_by_starting_error", "shutdown_by_running_error",
}


def instance_state(item):
    phase = item.get("status")
    if phase == "running":
        if item.get("start_mode") == "non_gpu":
            return "cpu"
        if item.get("start_mode") == "gpu":
            return "gpu"
    if phase in STOPPED:
        return "off"
    return "waiting"


def stock_text(item):
    idle = item.get("gpu_idle_num")
    total = item.get("gpu_all_num")
    return f"{idle if type(idle) is int else '—'} / {total if type(total) is int else '—'}"


def has_capacity(item):
    if item.get("charge_type") in {"yearly", "monthly", "weekly", "daily"}:
        stamp = item.get("expired_at") or {}
        if stamp.get("Valid"):
            try:
                until = datetime.fromisoformat(stamp["Time"])
                if until.tzinfo is None:
                    return False
                return until > datetime.now(timezone.utc)
            except (KeyError, TypeError, ValueError):
                return False
    needed = positive_int(item.get("req_gpu_amount"))
    idle = item.get("gpu_idle_num")
    return bool(needed and type(idle) is int and idle >= needed)


def send_email(settings, title, message):
    try:
        result = requests.post("https://api.emailjs.com/api/v1.0/email/send", timeout=12,
                               allow_redirects=False, json={
            "user_id": settings["email_public"],
            "service_id": settings["email_service"],
            "template_id": settings["email_template"],
            "template_params": {
                "to_email": settings["email_to"], "subject": title, "message": message,
            },
        })
        return 200 <= result.status_code < 300, f"EmailJS HTTP {result.status_code}"
    except requests.RequestException:
        return False, "EmailJS 网络请求失败"


def watch_instance(api, uuid, interval, automatic, restart, max_hourly, stop: Event,
                   report, alert, *, wait_limit=180, state_interval=3):
    """One run owns the stop/start sequence. A transport error after a write ends the run."""
    def read_target():
        items = api.instances(stop.is_set)
        return next((item for item in items if item["uuid"] == uuid), None)

    def wait_for(expected):
        end = monotonic() + wait_limit
        while monotonic() < end and not stop.wait(state_interval):
            item = read_target()
            if not item:
                continue
            report(item)
            if instance_state(item) == expected:
                return item
            if expected == "gpu" and item.get("status") in {
                "create_failed", "shutdown_by_starting_error", "shutdown_by_running_error",
            }:
                raise RuntimeError("平台报告开机或运行异常，请查看控制台。")
        return None

    def check_price(item):
        if not max_hourly or item.get("charge_type") != "payg":
            return
        price = positive_int(api.instance_snapshot(uuid).get("payg_price"))
        if not price:
            raise RuntimeError("无法确认实例当前时价，已暂停自动操作。")
        if price / 1000 > max_hourly:
            raise RuntimeError(f"当前实例时价 ¥{price / 1000:.3f}/时，超过设置上限。")

    seen_available = False
    missing = 0
    shut_down_here = False
    while not stop.is_set():
        try:
            item = read_target()
        except AuthError:
            raise
        except (requests.RequestException, ApiError, ValueError):
            if stop.wait(interval):
                break
            # A second failure ends the run so the UI can surface it.
            item = read_target()
        if stop.is_set():
            break
        if not item:
            missing += 1
            if missing >= 3:
                raise RuntimeError("连续三次未找到目标实例，请重新加载列表。")
            stop.wait(interval)
            continue
        missing = 0
        report(item)
        state = instance_state(item)
        name = item.get("name") or uuid
        available = has_capacity(item) and state in {"off", "cpu"}
        if state == "gpu" and automatic:
            return False, f"{name} 已在有卡模式运行"
        if available and not seen_available:
            alert("AutoDL 实例有空卡", f"{name} · 空闲 / 总量 {stock_text(item)}")
        seen_available = available
        if available and automatic and (state == "off" or restart):
            check_price(item)
            if stop.is_set():
                break
            if state == "cpu":
                try:
                    api.power(uuid, on=False)
                except (requests.RequestException, ValueError):
                    raise RuntimeError("关机请求结果未知，请在 AutoDL 控制台核对。") from None
                shut_down_here = True
                item = wait_for("off")
                if not item:
                    if stop.is_set():
                        break
                    raise RuntimeError("已发出关机请求，等待关机超时，请查看控制台。")
                if not has_capacity(item):
                    alert("AutoDL 实例等待空卡", f"{name} 已关机，空闲卡不足，继续等待。")
                    seen_available = False
                    stop.wait(interval)
                    continue
                check_price(item)
            if stop.is_set():
                break
            try:
                api.power(uuid, on=True)
            except ApiError as exc:
                if not stock_rejected(exc):
                    raise
                seen_available = False
                stop.wait(interval)
                continue
            except (requests.RequestException, ValueError):
                raise RuntimeError("开机请求结果未知，请在 AutoDL 控制台核对。") from None
            if wait_for("gpu"):
                return True, f"{name} 已成功以有卡模式开机"
            if not stop.is_set():
                raise RuntimeError("平台已接收开机请求，等待有卡运行超时，请查看控制台。")
            return False, "监控已停止。开机请求已发出，请在控制台确认状态。"
        stop.wait(interval)
    if shut_down_here:
        return False, "监控已停止。关机请求已发出，请在控制台确认实例状态。"
    return False, "监控已停止"
