# 部署脚本

部署到目标机器（NanoPi-R5S-LTS / RK3568 / aarch64 / Debian 11）的自动化脚本。

## 目录结构

```
deploy/
├── main.service            # 视觉程序的 systemd 服务定义
├── image_updater.service   # C 显示程序的 systemd 服务定义
├── install.sh              # 首次部署（裸机器 / 重装系统）
├── upgrade.sh              # ★ 现场升级:保留客户配置 + 自动回滚 + 完全离线
├── update-main.sh          # （旧）单纯换 binary,功能被 upgrade.sh 涵盖
├── uninstall.sh            # 卸载（回退到部署前）
└── README.md               # 本文件
```

## 🆕 推荐工作流(适用于现场没网络的客户)

**办公室(有网络的电脑):**
```bash
# 下载 release tarball
gh release download v0.2.0 --repo xiaoxiaobai123/toothpaste3Function
# 或浏览器从 https://github.com/xiaoxiaobai123/toothpaste3Function/releases 下

# 拷到 U 盘
cp toothpaste3Function-v0.2.0-aarch64.tar.gz /media/usb/
```

**现场 NanoPi(无网络):**
```bash
# 1. U 盘插上 NanoPi,挂载
sudo mount /dev/sdX1 /mnt/usb

# 2. 拷贝 + 解压
cp /mnt/usb/toothpaste3Function-v0.2.0-aarch64.tar.gz ~/
cd ~ && tar -xzf toothpaste3Function-v0.2.0-aarch64.tar.gz -C ./release/
cd release

# 3. 一键升级
sudo ./deploy/upgrade.sh
```

`upgrade.sh` 自动:

✅ **保留** `/home/pi/config.json` ← 客户配置不动
✅ **保留** `/home/pi/roi_coordinates_*.json` ← 工厂 ROI 不动
✅ **保留** `/home/pi/license.key` ← 授权不动
✅ **保留** `/home/pi/company_name.png` ← 客户改过的 logo 不动
✅ 备份老 binary 到 `main.bak.<时间戳>`
✅ 替换 main → 更新 systemd → 重启服务
✅ **8 秒内启动失败自动回滚**到老 binary

---

## 📥 各种场景

---

### 🆕 首次部署（裸机器、还没装过任何东西）

**在开发 VM 里**：
```bash
# 1. 编译
cd <项目根>
pyinstaller --clean -y main.spec
ls dist/main     # 确认产物存在
```

**把二进制和部署脚本传到 NanoPi**：
```bash
# 2. 推送文件（IP 换成实际的）
scp dist/main pi@192.168.x.x:/home/pi/
scp <image_updater的路径> pi@192.168.x.x:/home/pi/

# deploy 目录也传过去
rsync -av deploy/ pi@192.168.x.x:/home/pi/deploy/
```

**在 NanoPi 上跑部署脚本**：
```bash
# 3. ssh 到 NanoPi
ssh pi@192.168.x.x

# 4. 一键部署
cd /home/pi/deploy
chmod +x *.sh
./install.sh
```

脚本会自动做：
1. 检查二进制文件存在、`/dev/fb0` 可用
2. 停掉旧服务（如果在跑）
3. 备份可能存在的真实 `output_image.rgb565` 文件
4. 建 `/dev/shm/output_image.rgb565` 并设权限 666
5. 建软链接 `/home/pi/output_image.rgb565 → /dev/shm/output_image.rgb565`
6. 复制 service 文件到 `/etc/systemd/system/`
7. `systemctl daemon-reload` + `enable` + `start`
8. 健康检查，有问题报错

### 🔁 日常发版（只改代码，不改服务配置）

**开发 VM 编译**：
```bash
pyinstaller --clean -y main.spec
scp dist/main pi@192.168.x.x:/tmp/main.new
```

**NanoPi 上更新**：
```bash
ssh pi@192.168.x.x
cd /home/pi/deploy
./update-main.sh /tmp/main.new
```

脚本自动：
1. 停 main
2. 备份当前 `/home/pi/main` 到 `/home/pi/main.bak.<时间戳>`
3. 用新二进制替换
4. 启动 main
5. **如果启动失败 → 自动回滚**到上一版本
6. 如果启动成功但日志里有 Traceback / FATAL → 也回滚

### 🔙 卸载 / 回退

```bash
cd /home/pi/deploy
./uninstall.sh
```

停两个服务、删 service 文件、删软链接、恢复最新的 `output_image.rgb565.bak` 备份（如果有）。

---

## 常用运维命令

```bash
# 看服务状态
sudo systemctl status main.service
sudo systemctl status image_updater.service

# 看日志
tail -f /home/pi/my_app.log                      # 视觉程序
sudo journalctl -u image_updater -f              # C 显示程序
sudo journalctl -u main -f                       # systemd 启动日志

# 重启
sudo systemctl restart main.service
sudo systemctl restart image_updater.service

# 手动跑（调试）
sudo systemctl stop main.service
cd /home/pi && ./main
```

---

## 部署失败常见问题

### ① `Permission denied: 'output_image.rgb565'`

**原因**：`/dev/shm/output_image.rgb565` 不存在，Python 无法通过悬空软链接创建（`fs.protected_regular` 限制）。

**解决**：`install.sh` 的 Step 3 已处理，但如果碰到：
```bash
sudo touch /dev/shm/output_image.rgb565
sudo chmod 666 /dev/shm/output_image.rgb565
```

### ② `image_updater` 启动就死

**原因**：Systemd 启动 `image_updater` 时 `/dev/shm/output_image.rgb565` 还没建，`inotify_add_watch` 失败，C 代码 `exit(EXIT_FAILURE)`。

**解决**：`image_updater.service` 已经在 ExecStart 里用 `while true + sleep 5` 死循环保活，会自动重试。一般等几秒就好。

手动触发重启：
```bash
sudo systemctl restart image_updater.service
```

### ③ 日志里 `name 'logger' is not defined`

**原因**：用了老版本的二进制（修这 bug 之前的）。

**解决**：`./update-main.sh` 更新到最新 main。

### ④ 显示屏不刷新

**诊断顺序**：
```bash
# 1. 服务在跑吗
sudo systemctl status image_updater.service

# 2. tmpfs 文件有在被写入吗（mtime 应该随拍照更新）
stat /dev/shm/output_image.rgb565

# 3. 软链接对吗
ls -la /home/pi/output_image.rgb565
# 期望：... -> /dev/shm/output_image.rgb565

# 4. 重启显示程序
sudo systemctl restart image_updater.service
```

---

## 背景信息（为什么这么设计）

详见仓库根目录的 [`DISPLAY_CHANGES.md`](../DISPLAY_CHANGES.md)。
