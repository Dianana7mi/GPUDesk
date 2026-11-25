"""Offline regression checks for paid actions, the public API shape and instance transitions."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import requests

import gpuhawk_core as core
from cloning import CloneCoordinator, CloneSettings, matching_host, watch_clone
from instances import watch_instance


def vm(status="shutdown", mode="gpu", idle=1):
    return {"uuid": "instance-1", "name": "训练环境", "status": status, "start_mode": mode,
            "charge_type": "payg", "req_gpu_amount": 1, "gpu_idle_num": idle, "gpu_all_num": 8}


class FakeAutoDL:
    def __init__(self, states):
        self.states = iter(states)
        self.actions = []

    def instances(self, _cancelled):
        return [dict(vm(), uuid="other"), next(self.states)]

    def instance_snapshot(self, _uuid):
        return {"payg_price": 2399}

    def power(self, uuid, on):
        self.actions.append((uuid, on))


class WorkflowTests(unittest.TestCase):
    def monitor(self, api, *, restart=True, max_hourly=0, automatic=True, stop=None, report=None):
        return watch_instance(
            api, "instance-1", 0, automatic, restart, max_hourly, stop or Event(),
            report or (lambda _: None), lambda *_: None, wait_limit=1, state_interval=0,
        )

    def test_start_stopped_instance_and_wait_for_gpu_mode(self):
        api = FakeAutoDL([vm(), vm("starting"), vm("running")])
        self.assertTrue(self.monitor(api)[0])
        self.assertEqual(api.actions, [("instance-1", True)])

    def test_restart_only_after_shutdown_and_fresh_capacity(self):
        api = FakeAutoDL([vm("running", "non_gpu"), vm("shutting_down"), vm(idle=0),
                          vm(idle=0), vm(), vm("running")])
        self.assertTrue(self.monitor(api)[0])
        self.assertEqual(api.actions, [("instance-1", False), ("instance-1", True)])

    def test_monitor_only_and_cancel_do_not_write(self):
        api = FakeAutoDL([vm()])
        stop = Event()
        confirmed, _ = self.monitor(api, automatic=False, stop=stop, report=lambda _: stop.set())
        self.assertFalse(confirmed)
        self.assertEqual(api.actions, [])

    def test_hourly_cap_before_shutdown(self):
        api = FakeAutoDL([vm("running", "non_gpu")])
        with self.assertRaisesRegex(RuntimeError, "超过"):
            self.monitor(api, max_hourly=2.0)
        self.assertEqual(api.actions, [])

    def test_timeout_does_not_repeat_boot(self):
        api = FakeAutoDL([vm()])
        api.power = unittest.mock.Mock(side_effect=requests.Timeout)
        with self.assertRaisesRegex(RuntimeError, "结果未知"):
            self.monitor(api)
        api.power.assert_called_once_with("instance-1", on=True)

    def test_order_marker_blocks_retry_until_cleared(self):
        with TemporaryDirectory(ignore_cleanup_errors=False) as folder, patch.object(core, "APP_DIR", Path(folder)):
            submit = unittest.mock.Mock(side_effect=requests.Timeout)
            with self.assertRaises(core.OrderUncertain):
                core.order_guard(submit)
            with self.assertRaises(core.OrderUncertain):
                core.order_guard(submit)
            submit.assert_called_once()
            core.clear_pending_order()
            self.assertEqual(core.order_guard(lambda: "order-1"), "order-1")
            self.assertEqual(core.order_guard(lambda: "order-2"), "order-2")

    def test_rental_filters_unknown_price_and_keeps_dry_run_read_only(self):
        api = unittest.mock.Mock()
        api.gpu_table.return_value = {"RTX 4090": {"idle": 2}}
        api.machines.return_value = [
            {"machine_id": "unknown"}, {"machine_id": "expensive", "payg_price": 3000},
            {"machine_id": "eligible", "payg_price": 1800},
        ]
        args = SimpleNamespace(region=None, gpu="RTX 4090", num=1, max_price=2,
                               dry_run=True, image="img", private_image="")
        groups = {"region": ("测试区", ["test-1"])}
        self.assertIsNone(core.try_grab(api, args, groups, log=lambda _: None))
        api.create_payg.assert_not_called()
        args.dry_run = False
        with TemporaryDirectory() as folder, patch.object(core, "APP_DIR", Path(folder)):
            result = core.try_grab(api, args, groups, log=lambda _: None)
        self.assertEqual(result["machine_id"], "eligible")
        api.create_payg.assert_called_once_with("eligible", 1, "img", "")

    def test_authenticated_frontend_contract(self):
        api = core.AutoDL("fixture", sub_user=True)
        response = unittest.mock.Mock(status_code=200)
        response.json.return_value = {"code": "Success", "data": {"list": [vm()], "max_page": 1}}
        with patch.object(api.s, "request", return_value=response) as request:
            self.assertEqual(api.instances(), [vm()])
            self.assertTrue(request.call_args.args[1].endswith("/api/v1/sub_user/instance"))
            self.assertEqual(request.call_args.kwargs["json"]["page_index"], 1)
            api.power("instance-1", on=True)
            self.assertTrue(request.call_args.args[1].endswith("/api/v1/sub_user/instance/power_on"))
            self.assertEqual(request.call_args.kwargs["json"], {"instance_uuid": "instance-1"})
        api.s.close()

    def test_clone_waits_for_selected_host_and_submits_once(self):
        source = dict(vm(), region_sign="region-1", machine_id="old")
        snapshot = {"image": {"image_type": "public", "image_name": "torch", "cuda": 124}}
        old = {"machine_id": "old", "region_sign": "region-1", "gpu_name": "RTX 4090",
               "gpu_idle_num": 4, "payg_price": 1000, "highest_cuda_version": "12.9"}
        selected = dict(old, machine_id="target")
        other = dict(old, machine_id="other")
        settings = CloneSettings("instance-1", "RTX 4090", "target", 2, 2.0, True, 0, 0)
        api = unittest.mock.Mock()
        api.instances.return_value = [source]
        api.region_groups.return_value = {"region": ("测试区", ["region-1"])}
        api.instance_snapshot.return_value = snapshot
        api.machine_pages.side_effect = [[[
            old, other, dict(selected, gpu_idle_num=1), dict(selected, payg_price=1001),
            dict(selected, highest_cuda_version="12.3"),
        ]], [[selected]]]
        api.clone_payg.return_value = "new-instance"
        with TemporaryDirectory() as folder, patch.object(core, "APP_DIR", Path(folder)):
            result = watch_clone(api, settings, Event(), lambda _: None, lambda _: None)
        self.assertEqual(result["uuid"], "new-instance")
        api.clone_payg.assert_called_once_with(source, snapshot, "target", 2, True, 0)
        self.assertFalse(matching_host(dict(selected, region_sign="wrong"), source, snapshot, ["region-1"], settings))
        api.power.assert_not_called()

    def test_running_clone_source_does_not_submit(self):
        api = unittest.mock.Mock()
        api.instances.return_value = [vm("running")]
        settings = CloneSettings("instance-1", "RTX 4090", "", 1, 2.0, False, 0, 0)
        with self.assertRaisesRegex(RuntimeError, "保持关机"):
            watch_clone(api, settings, Event(), lambda _: None, lambda _: None)
        api.clone_payg.assert_not_called()

    def test_clone_request_uses_web_clone_endpoint_and_disk_settings(self):
        api = core.AutoDL("fixture", sub_user=True)
        with patch.object(api, "_call", return_value="new") as call:
            source = dict(vm(), private_image_uuid="priv")
            snapshot = {"image": {"image_type": "private"}}
            self.assertEqual(api.clone_payg(source, snapshot, "target", 2, True, 1024 ** 3), "new")
        args, kwargs = call.call_args
        self.assertEqual(args, ("POST", "/order/instance/clone/payg"))
        body = kwargs["json"]
        self.assertEqual(body["instance_uuid"], "instance-1")
        self.assertEqual(body["instance_info"]["private_image_uuid"], "priv")
        self.assertTrue(body["instance_info"]["copy_data_disk_after_clone"])
        self.assertEqual(body["price_info"]["expand_data_disk"], 1024 ** 3)
        self.assertEqual(body["price_info"]["num"], 2)
        api.s.close()

    def test_clone_tasks_for_one_source_wait_until_previous_copy_finishes(self):
        source = dict(vm(), region_sign="zone", machine_id="source")
        target = dict(vm(), uuid="previous", status="running")
        copying = dict(target, additional_info={"copy_data_disk_after_clone_status": "Running"})
        host = {"machine_id": "other", "region_sign": "zone", "gpu_name": "RTX 5090",
                "gpu_idle_num": 2, "payg_price": 1500}
        settings = CloneSettings("instance-1", "RTX 5090", "", 2, 4.0, True, 0, 0)
        coordinator = CloneCoordinator()
        coordinator.inflight["instance-1"] = "previous"
        api = unittest.mock.Mock()
        api.instances.side_effect = [[source], [source], [source, copying], [source, target]]
        api.region_groups.return_value = {"zone": ("测试区", ["zone"])}
        api.instance_snapshot.return_value = {"image": {"image_type": "public", "image_name": "torch"}}
        api.machine_pages.side_effect = lambda *_: iter([[host]])
        api.clone_payg.return_value = "next"
        logs = []
        with TemporaryDirectory() as folder, patch.object(core, "APP_DIR", Path(folder)):
            result = watch_clone(api, settings, Event(), lambda _: None, logs.append, coordinator)
        self.assertEqual(result["uuid"], "next")
        self.assertEqual(coordinator.inflight["instance-1"], "next")
        self.assertEqual(sum("上一笔" in line for line in logs), 2)
        api.clone_payg.assert_called_once()

    def test_two_card_offer_requires_eligible_single_host(self):
        api = unittest.mock.Mock()
        api.gpu_table.return_value = {"RTX 5090": {"idle": 2}}
        api.machines.side_effect = [[], [{"machine_id": "eligible", "payg_price": 2000}]]
        args = SimpleNamespace(region=None, gpu="RTX 5090", num=2, max_price=2,
                               dry_run=True, image="img", private_image="")
        groups = {"zone": ("测试区", ["zone"])}
        offers = []
        for _ in range(2):
            core.try_grab(api, args, groups, log=lambda _: None, on_offer=lambda _, value: offers.append(value))
        self.assertEqual(offers, [{}, {"machine": "eligible", "price": 2.0}])
        self.assertEqual(api.machines.call_args.args, (["zone"], "RTX 5090", 2))
        api.create_payg.assert_not_called()

    def test_busy_paid_submission_keeps_other_job_eligible_for_next_round(self):
        api = unittest.mock.Mock()
        api.gpu_table.return_value = {"A800": {"idle": 1}}
        api.machines.return_value = [{"machine_id": "eligible", "payg_price": 1000}]
        args = SimpleNamespace(region=None, gpu="A800", num=1, max_price=2,
                               dry_run=False, image="img", private_image="")
        groups = {"zone": ("测试区", ["zone"])}
        with patch.object(core, "order_guard", side_effect=core.OrderBusy):
            self.assertIsNone(core.try_grab(api, args, groups, log=lambda _: None))
        api.create_payg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
