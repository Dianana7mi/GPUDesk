# GPUDesk

一个独立的 Windows 桌面项目，整合 AutoDL 库存监控、自动租新机、已有容器实例的有卡开机和自动克隆。晨涧云监控免登录。

## 使用

双击 `GPUDesk.exe`。首次启动默认为提醒模式。退出活动监控前，关闭窗口会缩到系统托盘。可在托盘右键选择「退出」，程序会等待当前网络请求结束。

升级时从托盘退出旧版，再打开 `GPUDesk-3.exe`。新版继续使用当前 Windows 用户的 GPUDesk 设置和网页登录 Token。

**并行监控**

在「GPU 库存」选择 A800（以型号下拉框里的官方名称为准），卡数设为 1，点「添加并开始」。再选择 RTX 5090，卡数设为 2，再点一次。每条任务单独保留型号、卡数、地区、轮询频率、时价上限和运行模式。AutoDL 的多卡任务要求同一台主机有足够的卡。晨涧云的提醒按平台报告的库存数量判断。

「已有实例」也可反复添加任务。可并行监控不同实例的开机状态，或给同一个源实例添加多个不同的克隆目标。同源克隆会同时看库存，上一笔复制结束后才提交下一笔；每条已经确认的克隆任务都可能创建一台按量计费的新实例。

两个页面都有「监控任务」列表。每条任务可单独停止。租用、开机或克隆提交成功后，对应任务结束，其他任务继续。活动任务在本次运行中有效。

**连接 AutoDL**

1. 从「设置 → 打开 AutoDL」进入官网并正常登录。
2. 打开浏览器开发者工具的「网络」，刷新 AutoDL 实例列表页。在 AutoDL 的 `/api/v1/` 请求头中复制 `authorization` 的值。
3. 粘贴到 GPUDesk「设置 → 登录 Token」，点「保存并验证」。使用子账号时打开该选项。库存与已有实例功能共用这个网页登录 Token。

网页 Token 位于 `%LOCALAPPDATA%\GPUDesk\.env`。通知配置和界面偏好保存在当前 Windows 用户的应用设置中。请像保管密码一样保管 Token 和推送 Key，失效后从网页重新复制。

**新租实例或盯库存**

- 在「GPU 库存」按型号和地区看 AutoDL 的空闲量、总量、显存及趋势。双击库存行可查看单机 CPU、内存、CUDA、报价，也可加入指定机器提醒。
- 「只监控提醒」发现符合条件的资源后推送。晨涧云云主机和云容器也显示在库存表中。
- 设好卡数、每卡时价上限、地区和公有或私有镜像，切到自动模式，点「添加并开始」后确认。成功提交一台实例后退出任务。报价来自 AutoDL 网页库存接口，最终账单以控制台为准。
- 如果租用请求结果未知，程序挂起后续自动租用。先检查 AutoDL 实例列表和账单，再到「设置 → 租用记录」清除待确认状态。

**给已有实例开机**

1. 进入「已有实例」，点「加载实例」，按实例名称、ID 和规格选中一台。
2. 可以只提醒，也可以选择「有卡自动开机」。需要从无卡模式切换时，勾选「允许关机并从无卡模式切换」。关机会中断当前进程。
3. 自动模式可设置整个按量实例的小时价格上限。程序等待空闲卡数满足实例规格，已关机就有卡开机，无卡运行则先关机，等关机完成后重新查库存。实际进入有卡运行后发通知并停止任务。

**蹲新主机并自动克隆**

1. 保存进度，在 AutoDL 网页关机源实例。回到「已有实例」，点「加载实例」，选中源实例，再切到「有卡自动克隆」。
2. 选目标 GPU 和卡数，填 GPU 总时价上限。默认等待同地区任意匹配主机。可点「查找主机」，在下拉框锁定一台，即使它当前没有空卡。
3. 系统盘随克隆复制。默认同时克隆数据盘，若源实例使用付费扩容，确认窗口会列出目标实例的同等扩容量，磁盘日费单独计算。取消勾选时只克隆系统盘。
4. 点「添加并开始」并确认。发现满足条件的空卡后，程序提交按量克隆。平台接收后记录新实例 ID、发通知并结束这条任务。数据盘复制进度可在 AutoDL 控制台查看。克隆流程要求源实例保持关机，并保留源实例。

自动克隆使用 [AutoDL 同地区克隆流程](https://www.autodl.com/docs/migrate_instance_2/) 对应的网页接口。若请求结果未知，自动克隆和自动租用共用同一份待确认订单记录。

**通知**

桌面托盘通知和响铃适用于两条监控流程。设置中可填写 PushPlus、WxPusher、Server酱或企业微信群机器人，并发送测试通知。已有实例的开机成功通知也支持 EmailJS 邮件。模板参数为 `to_email`、`subject` 和 `message`。从桌面发送时，EmailJS 账号需允许非浏览器 API 请求。

## 从源码运行和打包

在 Windows PowerShell 进入源码目录。构建环境使用 Python 3.13。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install --no-deps PySide6-Fluent-Widgets==1.11.3
.\.venv\Scripts\python.exe gpuhawk.py
```

CLI 可列出 AutoDL 型号或运行库存任务，复用桌面保存的登录 Token，也支持 `AUTODL_TOKEN` 环境变量。

```powershell
.\.venv\Scripts\python.exe gpuhawk_core.py --list-gpus
.\.venv\Scripts\python.exe gpuhawk_core.py --gpu "RTX 4090" --region "西北B区" --num 1 --max-price 2.50 --dry-run
.\.venv\Scripts\python.exe gpuhawk_core.py --gpu "RTX 4090" --num 1 --max-price 2.50 --yes
```

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_workflows
.\.venv\Scripts\python.exe -m pip install pyinstaller==6.22.3
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm GPUHawk.spec
```

打包结果在 `dist\GPUDesk.exe`。

## 兼容性与来源

- 已有实例功能面向 AutoDL 标准容器，即网页「容器实例 → 实例列表」。AutoDL 公共目录和晨涧云库存已通过实际只读请求验证。租用、实例开关机与克隆使用模拟响应测试，实际账户需自行验证网页登录授权、库存和操作权限。
- AutoDL 的标准实例和库存走官网网页接口。平台更新接口时，可能需要同步更新客户端。EmailJS 及微信通知渠道需配置各自账号。
- 库存、界面和命令行在 [GPUHawk](https://github.com/Kingpython123/GPUHawk/tree/5532173d2e6c69d16e8ef047a48288a9bf58ffa8) 的 GPL-3.0 代码基础上修改。已有实例流程参考 [AutoDL GPU 自动抢机助手](https://github.com/wangzhao-desgin/autodl-gpu-monitor/tree/3579d10f974a34cf5db333484e22a78b62d28933)，由本项目用 AutoDL 请求和 Qt 界面实现。
- 项目与 AutoDL、晨涧云无官方关联。源码许可证见 [LICENSE](LICENSE)。
