"""Wait for a compatible host and submit one same-region AutoDL clone."""

from dataclasses import dataclass
import re
from threading import Lock

import requests

from gpuhawk_core import ApiError, OrderBusy, order_guard, positive_int, stock_rejected


GIB = 1024 ** 3
CLONEABLE_STATES = {"shutdown", "shutdown_by_starting_error", "shutdown_by_running_error"}


@dataclass(frozen=True)
class CloneSettings:
    source_uuid: str
    gpu: str
    machine_id: str
    num: int
    hourly_cap: float
    copy_data: bool
    expansion: int
    interval: int


class CloneCoordinator:
    def __init__(self):
        self.lock = Lock()
        self.inflight = {}

    def ready(self, source_uuid, items):
        """Call under the lock to release a source only after its prior target is ready."""
        target_uuid = self.inflight.get(source_uuid)
        if not target_uuid:
            return True
        target = next((item for item in items if item["uuid"] == target_uuid), None)
        if not target or target.get("status") not in {
            "created_clone_success", "running", "shutdown", "created_clone_failed", "create_failed",
        }:
            return False
        disk = (target.get("additional_info") or {}).get("copy_data_disk_after_clone_status")
        if str(disk).lower() in ("running", "pending", "queued", "starting"):
            return False
        if str(target.get("clone_status")).lower() in ("running", "pending", "queued", "starting"):
            return False
        self.inflight.pop(source_uuid)
        return True

    def wait_for_source(self, api, source_uuid, stop, interval, log):
        while not stop.is_set():
            with self.lock:
                if source_uuid not in self.inflight or self.ready(source_uuid, api.instances(stop.is_set)):
                    return True
            log("等待该实例上一笔克隆结束。")
            stop.wait(interval)
        return False


class CloneWaiting(RuntimeError):
    pass


def source_region(groups, source):
    sign = source.get("region_sign")
    for name, signs in groups.values():
        if sign in signs:
            return name, signs
    raise ValueError("无法确认源实例所在地区，请在 AutoDL 控制台检查。")


def _cuda_version(value):
    text = str(value or "").strip()
    if re.fullmatch(r"[1-9][0-9]{2}", text):
        number = int(text)
        return divmod(number, 10)
    parts = re.match(r"^(\d+)\.(\d+)", text)
    return (int(parts[1]), int(parts[2])) if parts else None


def matching_host(machine, source, snapshot, signs, settings):
    machine_id = machine.get("machine_id")
    if not machine_id or machine_id == source.get("machine_id"):
        return False
    if settings.machine_id and machine_id != settings.machine_id:
        return False
    if machine.get("region_sign") not in signs or machine.get("gpu_name") != settings.gpu:
        return False
    if machine.get("on_line") in (3, 4, 5, 6):
        return False
    if (positive_int(machine.get("gpu_idle_num")) or 0) < settings.num:
        return False
    if not (price := positive_int(machine.get("payg_price"))) or (
        settings.hourly_cap and price * settings.num > round(settings.hourly_cap * 1000)
    ):
        return False
    rent_mode = machine.get("rent_mode")
    if rent_mode:
        match = re.search(r"\d+", str(rent_mode))
        if not match or int(match[0]) <= 0:
            return False
        minimum = int(match[0])
        if settings.num < minimum or (not str(rent_mode).startswith("Base") and settings.num % minimum):
            return False
    if settings.expansion > (positive_int(machine.get("max_data_disk_expand_size")) or 0):
        return False
    if settings.copy_data:
        source_machine = snapshot.get("machine_info_snapshot") or {}
        source_base = positive_int(source_machine.get("max_instance_disk_size"))
        target_base = positive_int(machine.get("max_instance_disk_size"))
        if source_base and target_base and target_base < source_base:
            return False
    image = snapshot["image"]
    for key in ("cpu_arch", "chip_corp"):
        if image.get(key) and machine.get(key) and str(image[key]).lower() != str(machine[key]).lower():
            return False
    cuda = _cuda_version(image.get("cuda"))
    maximum = _cuda_version(machine.get("highest_cuda_version"))
    return not (cuda and maximum and maximum < cuda)


def watch_clone(api, settings, stop, report, log, coordinator=None):
    coordinator = coordinator or CloneCoordinator()

    def read_source(items, *, check_state=True):
        source = next((item for item in items if item["uuid"] == settings.source_uuid), None)
        if stop.is_set():
            return None
        if source is None:
            raise RuntimeError("账号下已找不到源实例，请重新加载实例列表。")
        report(source)
        if check_state and source.get("status") not in CLONEABLE_STATES:
            raise RuntimeError("源实例需要保持关机才能克隆。请关机后重新开始。")
        return source

    with coordinator.lock:
        source = read_source(api.instances(stop.is_set),
                             check_state=settings.source_uuid not in coordinator.inflight)
    if source is None:
        return None
    region, signs = source_region(api.region_groups(), source)
    snapshot = api.instance_snapshot(settings.source_uuid)
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("image"), dict):
        raise RuntimeError("无法取得源实例的镜像信息。")
    if settings.copy_data and (
        positive_int(snapshot.get("expand_data_disk_size"))
        or positive_int(source.get("expand_data_disk_size"))
        or 0
    ) != settings.expansion:
        raise RuntimeError("源实例数据盘扩容与启动时显示的不一致，请重新加载实例后确认。")
    log(f"{region} · 等待 {settings.gpu} × {settings.num}")

    rounds = 0
    while not stop.is_set():
        rounds += 1
        try:
            for batch in api.machine_pages(
                signs, settings.gpu, settings.num, settings.machine_id, stop.is_set
            ):
                candidates = sorted(
                    (m for m in batch if matching_host(m, source, snapshot, signs, settings)),
                    key=lambda m: m["payg_price"],
                )
                for machine in candidates:
                    machine_id = machine["machine_id"]
                    hourly = machine["payg_price"] * settings.num / 1000
                    if not coordinator.lock.acquire(blocking=False):
                        raise CloneWaiting("另一条克隆任务正在提交，等待下一轮。")
                    try:
                        items = api.instances(stop.is_set)
                        if stop.is_set():
                            return None
                        if not coordinator.ready(settings.source_uuid, items):
                            raise CloneWaiting("已有空卡，等待同一源实例上一笔克隆结束。")
                        current = read_source(items)
                        if current.get("region_sign") != source.get("region_sign") or (
                            settings.copy_data and (current.get("expand_data_disk_size") or 0) != settings.expansion
                        ):
                            raise RuntimeError("源实例地区或数据盘配置已变更，请重新加载后确认。")
                        log(f"发现 {machine.get('machine_alias') or machine_id}，GPU ¥{hourly:.3f}/时，正在提交克隆。")

                        def submit():
                            if stop.is_set():
                                return None
                            data = api.clone_payg(
                                current, snapshot, machine_id, settings.num, settings.copy_data, settings.expansion
                            )
                            if isinstance(data, dict):
                                data = data.get("instance_uuid") or data.get("uuid")
                            if not isinstance(data, str) or not data:
                                raise ValueError("克隆请求未返回新实例 ID")
                            return data

                        uuid = order_guard(submit)
                        if uuid is None:
                            return None
                        coordinator.inflight[settings.source_uuid] = uuid
                    except ApiError as exc:
                        if not stock_rejected(exc):
                            raise
                        log("该主机空卡已被占用，继续监控。")
                        continue
                    finally:
                        coordinator.lock.release()
                    return {"uuid": uuid, "machine_id": machine_id, "region": region, "hourly": hourly}
        except CloneWaiting as exc:
            log(str(exc))
            stop.wait(settings.interval)
            continue
        except OrderBusy:
            log("另一个任务正在提交订单，等待下一轮。")
            stop.wait(settings.interval)
            continue
        except requests.RequestException:
            log("查询库存失败，将在下一轮重试。")
        if rounds == 1 or rounds % 10 == 0:
            log(f"第 {rounds} 轮，暂未找到符合配置的空卡。")
        stop.wait(settings.interval)
    return None
