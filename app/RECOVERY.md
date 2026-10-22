# Khôi phục Lumi và dựng lại Android

Hướng dẫn cho chủ tài khoản Lumi đang dùng trên `dash.huyab.click/devices`.
Không cần Android Studio hoặc simulator để dashboard chạy bình thường.
Pipi đang dùng credentials lấy từ phiên app; **chưa có tự login/refresh token**.
Mất mạng không tự làm mất khóa. Chưa xác minh được thời hạn token hay chính sách
thu hồi phiên của Lumi; không coi bản backup là cách gia hạn một token đã hết hạn.

## Chọn đúng trường hợp

| Biểu hiện | Làm gì trước | Có cần simulator? |
|---|---|---|
| nixostail “Mất kết nối”, nhưng Lumi vẫn đọc được | Đây là ping máy tính, độc lập với xác thực Lumi | Không |
| Dashboard yêu cầu đăng nhập | Đăng nhập SSO của dashboard | Không |
| Lumi báo lỗi mạng/timeout | Kiểm tra mạng pipi, đợi lần đọc tiếp theo; không replay lệnh cũ | Chưa |
| HTTP 401/403 từ Lumi | Kiểm tra phiên app; capture status request mới | Có nếu không có phiên hợp lệ khác |
| Broker từ chối CONNECT hoặc không xác nhận lệnh | Đóng app/controller dùng chung client ID; nếu còn lỗi, làm full recovery | Có khi credentials MQTT không còn hợp lệ |
| Mất file cấu hình nhưng còn backup | Khôi phục backup, kiểm tra đọc trạng thái | Không nếu backup còn hợp lệ |
| Mất hết credentials | Đăng nhập lại, capture cả phiên và khóa; dùng chế độ `--apk --full` | Có |
| Sai certificate pin hoặc instruction mismatch | Dừng: app/certificate đã đổi; phải xác minh lại hook/pin | Bộ công cụ hiện tại không tự xử lý |

HTTP 403 cũng có thể là lỗi quyền hoặc bị chặn, không tự kết luận hết hạn.
Không có ping không chứng minh máy đã shutdown an toàn. Không tự cắt điện khi lỗi.

Capture/export mới vẫn dùng `~/.local/share/lumi-recovery` ngoài Git để giữ
kiểm tra an toàn của collector/exporter. Sau khi hoàn tất, có thể chuyển vào
`private/recovery/` trong repo local để lưu trữ; thư mục này bị Git bỏ qua.
Không dùng `git add -f` với bất kỳ dữ liệu private nào.

## 1. Kiểm tra mà không đổi trạng thái ổ cắm

Chạy trên Mac:

```bash
rtk proxy ssh pipi 'docker inspect -f "{{.State.Health.Status}}" glance-lumi'
rtk proxy ssh pipi 'curl -fsS -H "Host: dash.huyab.click" http://127.0.0.1:8766/lumi/api/status'
rtk proxy ssh pipi 'systemctl status job-nixostail-monitor.timer --no-pager'
```

`nixostail` là trạng thái ping của máy; `online` và `on` ở cấp ngoài là thông tin
Lumi. `checked_at` phải mới, `stale: false`, `error: null`. Một bản đọc thành công
chưa chứng minh MQTT vẫn điều khiển được. Chỉ thử bật/tắt khi đã đảm bảo tải điện an toàn.

## 2. Backup trước khi sửa

Các file bí mật trên pipi: `/opt/config/lumi/{runtime.json,status-request.json,signing-key.pem,mqtt-peer.pem,d1.json}`.
Không `cat` chúng ra terminal/chat, không đưa vào Git hoặc D1. `d1.json` chỉ phục vụ
đồng bộ lịch sử, không phải token Lumi và không thay khi gia hạn Lumi.

```bash
umask 077
LUMI_PRIVATE="$HOME/.local/share/lumi-recovery"
rtk proxy mkdir -p "$LUMI_PRIVATE"
rtk proxy chmod 700 "$LUMI_PRIVATE"
LUMI_BACKUP="$LUMI_PRIVATE/pipi-config-$(date +%Y%m%d-%H%M%S).tgz"
rtk proxy ssh pipi 'tar -C /opt/config -czf - lumi' > "$LUMI_BACKUP"
rtk proxy chmod 600 "$LUMI_BACKUP"
```

Archive này chứa secrets dạng rõ bên trong; cất thêm một bản trong nơi lưu trữ
được mã hóa do bạn quản lý. Không upload archive lên GitHub. Khi cần dùng làm base:

```bash
LUMI_BASE="$LUMI_PRIVATE/base-$(date +%Y%m%d-%H%M%S)"
rtk proxy mkdir -m 700 "$LUMI_BASE"
rtk proxy tar -xzf "$LUMI_BACKUP" -C "$LUMI_BASE"
LUMI_BASE="$LUMI_BASE/lumi"
```

Nếu không còn backup, bỏ qua bước này và dùng `--apk --full` ở mục 6.
Khôi phục credentials không đòi hỏi xóa SQLite hoặc lịch sử D1.

## 3. Dựng lại Android trên Mac Apple Silicon

Bản đã kiểm chứng: Lumi IoT **2.1.0**, package `com.lumi.thing`, Android 35
**Google APIs ARM64** (không dùng Google Play image vì cần `adb root`), Pixel 6,
RAM 2 GB, keyboard bật, AVD `Lumi_IoT_API_35`. Studio cũ là
`2026.2.1.8,rabbit1`; Brew có thể cài bản mới hơn, không ảnh hưởng offset của app
nếu vẫn cài đúng APK đã kiểm chứng. Cấu hình và checksum nằm ở
[`tools/android-build.json`](tools/android-build.json).

Đã gỡ Studio, SDK và AVD khỏi Mac để giải phóng dung lượng. Phiên đăng nhập trong
AVD đã mất; dựng lại cần đăng nhập bằng tài khoản của bạn. Các nguồn được giữ:

- APK và split APK: `~/Documents/personal-projects/lumi-iot-api/private/apk/Lumi-IoT-2.1.0/` (khoảng 66 MB).
- Công cụ/capture cũ và bản private export: `~/Documents/personal-projects/lumi-iot-api/`.
- Credentials đang chạy: pipi `/opt/config/lumi/`.

Các đường dẫn Mac là bản giữ riêng, không phải file được push cùng repo. Nếu mất
APK, cần lấy lại đúng bản từ nguồn tin cậy, kiểm checksum trước khi dùng hook.
Không cài APK mới rồi bỏ qua kiểm tra instruction mismatch.

```bash
rtk proxy brew install --cask android-studio android-commandlinetools
export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
export ANDROID_HOME="$HOME/Library/Android/sdk"
rtk proxy sdkmanager --sdk_root="$ANDROID_HOME" --licenses
rtk proxy sdkmanager --sdk_root="$ANDROID_HOME" 'platform-tools' 'emulator' 'platforms;android-35' 'system-images;android-35;google_apis;arm64-v8a'
rtk proxy avdmanager create avd -n Lumi_IoT_API_35 -k 'system-images;android-35;google_apis;arm64-v8a' -d pixel_6
```

Xác nhận điều khoản SDK bằng tay. Không dùng `--force` để ghi đè một AVD đang có.
Mở Android Studio → Device Manager → Edit AVD: RAM 2048 MB, bật hardware keyboard.
Chạy ở terminal riêng; GPU software đã giải quyết tình trạng app treo ở máy này:

```bash
rtk proxy "$ANDROID_HOME/emulator/emulator" -avd Lumi_IoT_API_35 -gpu software -no-snapshot
```

Terminal thứ hai: đặt lại `ANDROID_HOME` như trên, kiểm tra đúng serial rồi cài
toàn bộ split APK, không chỉ cài `com.lumi.thing.apk`:

```bash
rtk proxy python3 - <<'PY'
import hashlib, json
from pathlib import Path
expected = json.loads(Path('tools/android-build.json').read_text())['apk_sha256']
folder = Path.home() / 'Documents/personal-projects/lumi-iot-api/private/apk/Lumi-IoT-2.1.0'
for name, digest in expected.items():
    assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == digest, name
print('APK checksums OK')
PY
rtk proxy "$ANDROID_HOME/platform-tools/adb" devices -l
rtk proxy "$ANDROID_HOME/platform-tools/adb" -s emulator-5554 wait-for-device
rtk proxy "$ANDROID_HOME/platform-tools/adb" -s emulator-5554 install-multiple "$HOME/Documents/personal-projects/lumi-iot-api/private/apk/Lumi-IoT-2.1.0/"*.apk
```

Nếu serial khác `emulator-5554`, thay đúng serial ở các bước sau. Mở app và đăng
nhập thủ công. Không ghi mật khẩu/OTP vào script. Dùng tab thông số/chi tiết để
kiểm tra tài khoản nhìn thấy đúng ổ cắm, chưa bấm công tắc.

## 4. Chuẩn bị capture đúng phiên bản

Các lệnh sau chạy từ `app/` của repo `lumi-iot-api`. Không chạy collector và
controller đồng thời với cùng client ID. Dừng riêng Lumi trên pipi trong lúc
thu hồi phiên; việc này tạm dừng thu thập thông số, không thay đổi relay vật lý:

```bash
rtk proxy ssh pipi 'docker stop glance-lumi'
umask 077
LUMI_PRIVATE="$HOME/.local/share/lumi-recovery"
LUMI_SDK="$HOME/Library/Android/sdk"
rtk proxy mkdir -p "$LUMI_PRIVATE"
rtk proxy chmod 700 "$LUMI_PRIVATE"
rtk proxy python3 -m venv "$LUMI_PRIVATE/venv"
rtk proxy "$LUMI_PRIVATE/venv/bin/pip" install 'frida==17.23.2' 'frida-tools==14.11.0'
```

Giữ app đóng khi chưa capture. Frida server cùng bản ARM64 còn ở thư mục local
`~/Documents/personal-projects/lumi-iot-api/tools/frida-server-17.23.2-android-arm64`.
Nếu cần tải lại, dùng release chính thức
[Frida 17.23.2](https://github.com/frida/frida/releases/tag/17.23.2), file
`frida-server-17.23.2-android-arm64.xz`. So SHA256 archive và binary với
`android-build.json` trước khi chạy; không lấy binary từ repo khác.

```bash
LUMI_FRIDA="$HOME/Documents/personal-projects/lumi-iot-api/tools/frida-server-17.23.2-android-arm64"
rtk proxy "$LUMI_SDK/platform-tools/adb" -s emulator-5554 root
rtk proxy "$LUMI_SDK/platform-tools/adb" -s emulator-5554 wait-for-device
rtk proxy "$LUMI_SDK/platform-tools/adb" -s emulator-5554 push "$LUMI_FRIDA" /data/local/tmp/lumi-frida-server-17.23.2
rtk proxy "$LUMI_SDK/platform-tools/adb" -s emulator-5554 shell chmod 700 /data/local/tmp/lumi-frida-server-17.23.2
rtk proxy "$LUMI_SDK/platform-tools/adb" -s emulator-5554 shell '/data/local/tmp/lumi-frida-server-17.23.2 -l 127.0.0.1:27042 -D'
rtk proxy "$LUMI_SDK/platform-tools/adb" -s emulator-5554 forward tcp:27043 tcp:27042
```

Chỉ bind loopback. Nếu `adb root` báo production build không cho phép, đã chọn
sai image; dùng Google APIs như mục 3. Nếu app treo, dừng collector, khởi động lại
đúng Frida server/emulator bằng GPU software; không clear app data để thử vận may.

### Lấy khóa nếu phiên MQTT/khóa đã đổi

Bỏ qua bước này nếu chỉ cập nhật HTTP status request với bộ khóa cũ còn hợp lệ.
Script quan sát kết quả giải mã khóa của app, không ghi đè hàm trả về.
Log **có private key**, nên phải chuyển thẳng vào file private:

```bash
LUMI_KEY_LOG="$LUMI_PRIVATE/key-$(date +%Y%m%d-%H%M%S).log"
rtk proxy "$LUMI_SDK/platform-tools/adb" -s emulator-5554 shell am force-stop com.lumi.thing
rtk proxy "$LUMI_PRIVATE/venv/bin/frida" -H 127.0.0.1:27043 -f com.lumi.thing -l tools/read-signing-key.js -q -t 90 > "$LUMI_KEY_LOG" 2>&1
rtk proxy chmod 600 "$LUMI_KEY_LOG"
```

Trong 90 giây, mở app/hoàn tất đăng nhập nếu được yêu cầu. Không `cat` log hoặc
dán log vào chat. Exporter sẽ kiểm tra có đúng một khóa và chữ ký khớp command
capture. Nếu không thu được khóa, không đoán khóa; kiểm tra đúng app/build/hook.

### Capture HTTP và MQTT

Chạy sau khi lần lấy khóa đã kết thúc. Lần này khởi động app qua collector để bắt
được MQTT CONNECT đầu phiên:

```bash
LUMI_CAPTURE="$LUMI_PRIVATE/capture-$(date +%Y%m%d-%H%M%S)"
rtk proxy "$LUMI_SDK/platform-tools/adb" -s emulator-5554 shell am force-stop com.lumi.thing
rtk proxy "$LUMI_PRIVATE/venv/bin/python" tools/capture.py "$LUMI_CAPTURE" com.lumi.thing 180
```

Đợi `Capture ready: runtime instruction checks passed`, mở chi tiết thiết bị để
app gọi status API. Collector tự dừng sau 180 giây; cũng có thể Ctrl+C để detach.

**Chỉ status refresh:** không cần bấm công tắc.

**Full recovery:** cần một command ON và phản hồi xác nhận tương ứng trong cùng
capture. Chỉ làm khi bạn đã chuẩn bị tải điện và chủ động cho phép thao tác vật lý;
ví dụ tháo tải khỏi ổ cắm trước. Bắt đầu capture khi ổ cắm đang Tắt, bấm đúng một
lần để Bật và chờ app xác nhận. Không dùng máy đang chạy làm tải thử, không tự
Tắt→Bật máy để lấy capture. Exporter không gửi command thay bạn.

Hook chỉ hỗ trợ Flutter engine/build ghi trong `capture.js`. Instruction mismatch
hoặc certificate khác: dừng, xác minh lại offsets và chứng chỉ; không tắt kiểm tra
TLS/pin. Công cụ không tự thích ứng với app version/certificate mới.

## 5. Export khi còn bản backup

Chỉ cập nhật HTTP request, giữ nguyên MQTT/key/certificate của base:

```bash
LUMI_BUNDLE="$LUMI_PRIVATE/bundle-$(date +%Y%m%d-%H%M%S)"
rtk proxy python3 tools/recover.py export --base "$LUMI_BASE" --capture "$LUMI_CAPTURE/events.jsonl" --output "$LUMI_BUNDLE"
```

Nếu MQTT/khóa đã đổi, dùng capture đầy đủ và log khóa mới:

```bash
LUMI_BUNDLE="$LUMI_PRIVATE/bundle-full-$(date +%Y%m%d-%H%M%S)"
rtk proxy python3 tools/recover.py export --base "$LUMI_BASE" --capture "$LUMI_CAPTURE/events.jsonl" --full --key-log "$LUMI_KEY_LOG" --output "$LUMI_BUNDLE"
```

Không đổi `device_id` để vượt lỗi scope. Full recovery với base chỉ nhận đúng ổ
cắm cũ, CONNECT được chấp nhận và ON có phản hồi khớp request ID.

## 6. Export nếu mất toàn bộ credentials

Sau khi đăng nhập và làm full capture, có thể dựng bundle từ APK đã kiểm checksum,
chứng chỉ public đã quan sát trong app và khóa mới, không cần base:

```bash
LUMI_BUNDLE="$LUMI_PRIVATE/bundle-rebuilt-$(date +%Y%m%d-%H%M%S)"
rtk proxy python3 tools/recover.py export --apk "$HOME/Documents/personal-projects/lumi-iot-api/private/apk/Lumi-IoT-2.1.0/com.lumi.thing.apk" --capture "$LUMI_CAPTURE/events.jsonl" --full --key-log "$LUMI_KEY_LOG" --output "$LUMI_BUNDLE"
rtk proxy python3 tools/recover.py validate "$LUMI_BUNDLE"
```

Exporter yêu cầu đúng một ổ cắm trong ON command, đúng API host, certificate đã
thấy trong capture và chữ ký hợp lệ. Nó ghi bốn file mode 600 trong thư mục 700,
từ chối output trong Git hoặc ghi đè thư mục có sẵn; không mang `d1.json` vào bundle.
Kiểm tra offline chứng minh cấu trúc/chữ ký, **không chứng minh token còn được Lumi chấp nhận**.
Nếu base cũ đã bỏ JWT capture khỏi template, validator chỉ thử ký/verify bằng
khóa local; full capture có JWT mới mới đối chiếu được khóa với chữ ký của app.

## 7. Đưa bundle lên pipi và xác minh

Đóng app/collector trước, sau đó tải bundle qua SSH. Giữ backup phía pipi để có
thể rollback. Dùng staging riêng và không đổi cả thư mục `/opt/config/lumi`, vì
cần giữ `d1.json` và quyền truy cập hiện tại.

```bash
rtk proxy "$LUMI_SDK/platform-tools/adb" -s emulator-5554 shell am force-stop com.lumi.thing
rtk proxy python3 tools/recover.py validate "$LUMI_BUNDLE"
rtk proxy ssh pipi 'mkdir -m 700 /opt/config/lumi-recovery-upload'
rtk proxy scp "$LUMI_BUNDLE"/{runtime.json,status-request.json,signing-key.pem,mqtt-peer.pem} pipi:/opt/config/lumi-recovery-upload/
rtk proxy ssh pipi 'bash -s' <<'SH'
set -euo pipefail
backup="/opt/config/lumi-before-recovery-$(date +%Y%m%d-%H%M%S)"
cp -a /opt/config/lumi "$backup"
docker stop glance-lumi
for name in runtime.json status-request.json signing-key.pem mqtt-peer.pem; do
  install -m 600 -o 10001 -g 10001 "/opt/config/lumi-recovery-upload/$name" "/opt/config/lumi/$name"
done
docker start glance-lumi
printf 'Rollback directory: %s\n' "$backup"
SH
```

Nếu staging đã tồn tại, không ghi đè mù; kiểm tra đó là lần recovery nào và chọn
tên staging mới, sửa cùng tên trong ba lệnh. Nếu script dừng giữa chừng, rollback
ngay từ đường dẫn backup (tìm tên thư mục bằng `ls -d`, không đọc nội dung secrets).
Chờ khoảng 20 giây, chạy lệnh GET ở mục 1 và mở Devices. Kiểm tra:

1. Trạng thái mới, không lỗi xác thực, hiển thị đúng trạng thái ổ cắm thực tế.
2. V/A/W có dữ liệu khi thiết bị cung cấp; số 0 không phải lỗi.
3. Lịch sử tiếp tục có mẫu mới; `d1.json` vẫn giữ nguyên.
4. Chưa thử điều khiển thì ghi rõ **MQTT chưa kiểm chứng live**. Chỉ thử một lệnh
   khi tải đã an toàn, kiểm tra xác nhận và không tự retry nếu timeout.

Không cần rebuild container hay restart tunnel để cập nhật bốn file credentials.

## 8. Rollback và dọn capture

Nếu bản mới lỗi, dừng Lumi, khôi phục **bốn file** từ thư mục backup đã in ở mục 7
bằng `install -m 600 -o 10001 -g 10001`, rồi `docker start glance-lumi`. Không xóa
database; không thay `d1.json`. Backup có token đã hết hạn sẽ không khôi phục được
quyền đăng nhập, nhưng giữ lại trạng thái trước thay đổi để chẩn đoán.

Điền đúng tên backup đã ghi ở mục 7; lệnh kiểm tra đủ bốn file trước khi dừng dịch vụ:

```bash
rtk proxy ssh pipi 'bash -s' <<'SH'
set -euo pipefail
backup="/opt/config/lumi-before-recovery-YYYYMMDD-HHMMSS"
for name in runtime.json status-request.json signing-key.pem mqtt-peer.pem; do
  test -s "$backup/$name"
done
docker stop glance-lumi
for name in runtime.json status-request.json signing-key.pem mqtt-peer.pem; do
  install -m 600 -o 10001 -g 10001 "$backup/$name" "/opt/config/lumi/$name"
done
docker start glance-lumi
SH
```

Sau khi đã kiểm chứng, cất bundle private vào nơi backup mã hóa, xóa staging
upload và các bản plaintext dư thừa theo chính sách của bạn. Captures, log khóa,
APK, `.venv` và private bundle không nằm trong Git.

Cleanup simulator sau capture:

```bash
rtk proxy "$LUMI_SDK/platform-tools/adb" -s emulator-5554 shell am force-stop com.lumi.thing
rtk proxy "$LUMI_SDK/platform-tools/adb" -s emulator-5554 shell 'pidof lumi-frida-server-17.23.2'
```

Kiểm tra PID thuộc đúng executable `/data/local/tmp/lumi-frida-server-17.23.2`,
rồi kill riêng PID đó. Không kill toàn bộ adb/Frida của công việc khác.

```bash
rtk proxy "$LUMI_SDK/platform-tools/adb" -s emulator-5554 forward --remove tcp:27043
rtk proxy "$LUMI_SDK/platform-tools/adb" -s emulator-5554 unroot
rtk proxy "$LUMI_SDK/platform-tools/adb" -s emulator-5554 wait-for-device
rtk proxy "$LUMI_SDK/platform-tools/adb" -s emulator-5554 shell id
rtk proxy "$LUMI_SDK/platform-tools/adb" -s emulator-5554 emu kill
```

`id` phải về shell user. Không để app chạy cùng controller pipi.

## Phạm vi đã kiểm chứng của bộ hướng dẫn

- Kiểm tra cấu hình AVD thực tế trước khi gỡ; lưu checksum APK/server và phiên bản.
- Export status-only, full với base và full từ APK trên capture đã có: thành công,
  đối chiếu khóa/chữ ký offline. Không đưa bundle thử lên pipi.
- Test synthetic: HTTP bị chia gói, MQTT reconnect, thiếu capture/cert, sai host,
  sai thiết bị/khóa, quyền file 600/700 và chặn ghi vào Git.
- **Chưa diễn tập lại từ AVD trắng, chưa thực hiện login/refresh mới và chưa thử
  bật/tắt bằng bundle mới.** Khi dựng lại phải kiểm chứng live theo mục 7.

```bash
rtk proxy python3 test_recovery.py
```
