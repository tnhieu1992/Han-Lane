# HanLane

Hackathon MongoDB + Datadog, 5 ngày. HanLane là lớp dữ liệu thời gian thực cho xe tự lái ở Đà Nẵng: xe giả lập bằng SUMO bắn telemetry vào MongoDB Atlas, gateway phát hiện sự cố và chọn xe cần báo, worker chuyển cảnh báo ngược lại cho xe phía sau. Datadog đo trọn vòng từ bản ghi kích hoạt luật tới lúc xe nhận lệnh (t5 trừ t0), mục tiêu p95 dưới 1 giây.

Đây là loại cảnh báo V2N (xe tới máy chủ) cho nguy hiểm phía trước 50 đến 150 m: xe dừng trên đường, kẹt xe phía trước. 3GPP TS 22.185 cho V2N tới 1.000 ms. Không phải tránh va chạm tức thời: xe bám sát dưới 2 giây không nằm trong phạm vi.

Không xây AI lái xe. Thứ được chấm là dữ liệu, độ trễ vòng phản hồi, khả năng quan sát, và tác dụng đo được bằng A/B.

File này là nguồn chuẩn. Bản thiết kế đầy đủ có hình nằm ở `docs/HanLane_ho-so-y-tuong.pdf`; chỉ mở chương liên quan khi cần chi tiết, không đọc cả file mỗi phiên. Prompt theo ngày ở `docs/prompts.md`.

## Cách làm việc với tôi

- Trao đổi với tôi bằng tiếng Việt. Code, comment, tên biến, commit message và README viết bằng tiếng Anh.
- Việc lớn: lên plan trước, chờ tôi duyệt rồi mới làm.
- Mỗi mốc kết thúc bằng một lệnh kiểm tra chạy được, và báo kết quả thật (số đo, log). Không báo "đã xong" khi chưa chạy.
- Không tự ý đổi kiến trúc hay thêm dịch vụ mới (Kafka, Redis...) khi chưa hỏi.
- Không commit secret. Key nằm trong `.env` (đã có trong `.gitignore`); giữ `.env.example` cập nhật.
- Commit nhỏ, sau mỗi mốc chạy được.

## Kiến trúc

```
sim-bridge (SUMO + TraCI, bước 0,2 s) --batch mỗi giây--> ingest-gateway (FastAPI)
ingest-gateway: chạy luật ngay khi nhận batch, trên trạng thái trong RAM
ingest-gateway --insert_many telemetry, song song, không chờ ack--> MongoDB Atlas
ingest-gateway --khi luật bắn: chọn xe nhận, insert_one events ngay (kèm recipients, trace)--> events
events --change stream (chỉ insert)--> advisory-worker --HTTP--> sim-bridge (hàng đợi)
vòng TraCI áp slowDown / changeLane ở bước kế tiếp; worker ghi advisories sau, không chặn
ingest-gateway --WebSocket--> web (MapLibre)
Mọi service Python --> Datadog Agent (APM, DogStatsD, log); web dùng RUM nếu kịp
```

| Service | Thư mục | Ghi chú |
| --- | --- | --- |
| sim-bridge | `sim/` | Kịch bản SUMO khu trung tâm hai bờ sông Hàn. Bước 0,2 s, đọc mỗi giây bằng TraCI subscription (không gọi từng hàm cho từng xe). Đổi tọa độ bằng `sumolib` (`net.convertXY2LonLat`). HTTP endpoint nhận cảnh báo vào hàng đợi thread-safe; vòng TraCI lấy ra áp ở đầu mỗi bước, vì TraCI chỉ cho một luồng điều khiển. Mỗi giây đánh dấu một bản ghi probe. |
| ingest-gateway | `gateway/` | Nhận batch, giữ trạng thái live và cửa sổ trượt trong RAM, chạy luật, chọn xe nhận, ghi MongoDB, đẩy WebSocket, chống trùng batch gửi lại. Nhận lệnh từ web (tạo sự cố, dọn sự cố, `WRITE_MODE`) và chuyển cho sim-bridge. |
| advisory-worker | `worker/` | Change stream trên `events` với `[{$match: {operationType: "insert"}}]`, giữ resume token. Gửi lệnh cho các xe trong `recipients`, ghi `advisories` sau. Không truy vấn telemetry trên đường nóng. |
| web | `web/` | MapLibre GL JS, bản đồ nền Việt Nam (TrackAsia hoặc Vietmap), chỉ đọc từ gateway |
| db | `db/` | `init.py` tạo collection và index, chạy lại nhiều lần không lỗi; các pipeline aggregation |
| scripts | `scripts/` | `incident.py`, `rush_hour.py`, `ab_run.py`, `measure_m0.py`, `dd_apply.py` |
| datadog | `datadog/` | Dashboard, monitor, SLO dạng JSON |

Stack: Python 3.11+, FastAPI, PyMongo (+ `zstandard`), ddtrace 3.x (`from ddtrace.trace import tracer`), datadog (DogStatsD), eclipse-sumo, traci, sumolib; frontend Vite + MapLibre GL JS; Docker Compose cho Datadog Agent và các service.

## Ràng buộc bắt buộc

Atlas M0 (free tier):

- Tối đa 100 thao tác đọc ghi mỗi giây. Không bao giờ ghi từng document. sim-bridge gửi một batch mỗi giây, gateway ghi ngay batch đó bằng `insert_many(..., ordered=False)`; không có lớp gom batch thứ hai. Theo serverStatus, một lệnh insert nhiều document tính là một thao tác; `measure_m0.py` phải xác nhận trên M0 (so 150 insert lẻ mỗi giây với một insert_many 300 bản ghi mỗi giây, xem có khựng 1 giây không).
- `WRITE_MODE=batch|single`: `single` chỉ để quay cảnh thử tải, cho thấy vượt trần.
- 512 MB tính cả index. Hai giờ dữ liệu thô chưa nén khoảng 860 MB (300 xe, khoảng 400 byte mỗi bản ghi), nên TTL đứng được hay không phụ thuộc tỉ lệ nén của time series; đo ngày 1 rồi chốt TTL. Phát triển với khoảng 50 xe.
- 10 GB vào và 10 GB ra mỗi 7 ngày, gồm cả ngày demo. Bật `compressors="zstd"`. Frontend không truy vấn Atlas. Tắt sim khi không test. Xem "Network bytes in" của Atlas mỗi ngày. Truy vấn từ MCP và Compass cũng tính vào số thao tác và băng thông.
- Bộ nhớ sort 32 MB, không có allowDiskUse. Aggregation dựa vào index, cửa sổ thời gian ngắn, `$project` bớt field trước `$setWindowFields`.

MongoDB time series:

- Không hỗ trợ change stream. Sự kiện ghi ra collection thường `events`.
- `$geoNear` là stage không gian duy nhất, bắt buộc `key`, KHÔNG nhận field `query`; lọc thời gian đặt sau `$geoNear`. Không dùng `$geoNear` trên đường nóng; chỉ cho phân tích sau sự cố, `maxDistance` nhỏ, chạy explain trước.
- Không có unique index trên time series; chống trùng làm ở gateway.
- `$merge` chỉ đi từ time series sang collection thường.
- `metaField` chỉ chứa thứ gần như không đổi (`vid`, `type`). Không đưa `zone`, `edge`, `lane` vào meta.
- Index trên field đo lường làm việc ở mức bucket (min, max), nên index theo `zone` gần như không lọc được; tổng hợp theo khu dùng rollup.
- TTL xóa theo bucket (tối đa 1.000 bản ghi mỗi bucket, task chạy mỗi 60 s); ở 1 Hz mỗi xe, dữ liệu sống thêm khoảng 17 phút sau mốc TTL.
- Thời gian lưu là BSON Date có timezone, không phải chuỗi.

Datadog:

- Tích hợp Atlas với Datadog và Database Monitoring cần M10 trở lên, không dùng được ở đây. Nhìn MongoDB qua APM span của driver (ddtrace tự patch PyMongo).
- Site: `ap1.datadoghq.com`. Trial 14 ngày.
- Percentile của distribution metric tắt mặc định và chỉ tính từ lúc bật: `dd_apply.py` bật `include_percentiles` ngay ngày 1 cho `loop.latency_ms`, `detect.lag_ms`, `ingest.lag_ms`, `mongo.batch_size`.
- Không dùng anomaly monitor cho metric mới tạo. Không dùng synthetic test (không gọi được localhost nếu không có private location); dùng no-data monitor.

## Data model

| Collection | Loại | Index | Giữ |
| --- | --- | --- | --- |
| `telemetry` | time series, `timeField: ts`, `metaField: meta`, granularity `seconds` | `{meta.vid: 1, ts: -1}`, `{loc: "2dsphere"}` | TTL 7200 s (chốt lại sau khi đo) |
| `rsu_readings` | time series, meta `{rsu_id, junction}` | `{meta.rsu_id: 1, ts: -1}` | TTL 21600 s |
| `events` | thường | `{ts: -1}`, `{loc: "2dsphere"}`, `{type: 1, zone: 1, ts: -1}`, `{status: 1}` | TTL 7 ngày |
| `advisories` | thường | `{event_id: 1}`, `{ts: -1}` | TTL 7 ngày (trên `ts`) |
| `road_segments` | thường | `{geometry: "2dsphere"}`, `{edge: 1}`, `{lane_id: 1}` | cố định |
| `rollups_1m` | thường, field `zone, edge, minute, avgSpeed, vehicles` (+ field RSU) | unique `{edge: 1, minute: 1}` (cho `$merge on`), `{zone: 1, minute: -1}` | TTL 3 ngày trong hackathon |

Bản ghi telemetry mẫu (mongosh):

```js
{
  ts: ISODate("2026-10-20T14:05:03Z"),      // giờ giả lập neo vào epoch, cách nhau đúng 1 s
  t0: ISODate("2026-10-20T14:05:03.012Z"),  // giờ thật lúc đọc, chỉ để đo độ trễ
  meta: { vid: "moto-0012", type: "motorbike" },
  loc: { type: "Point", coordinates: [108.2272, 16.0612] },
  lon: 108.2272, lat: 16.0612,              // số, để $fill nội suy khi tua lại
  zone: "cau-rong", edge: "e123", lane: "e123_1", pos: 84.5,
  speed: 8.4, accel: -0.6, heading: 92, latOffset: 0.4, leaderGap: 12.5,
  signals: { brake: false, hazard: false },
  nextTls: { state: "G", dist: 140 }        // từ getNextTLS
}
```

Document `events`: `type`, `status` (`open` / `closed`), `ts` (t3), `sensor_ts` (t0 của bản ghi kích hoạt), `loc`, `edge`, `lane`, `zone`, `vid`, `severity`, `recipients` (`[{vid, action}]`), `trace` (context đã inject). Probe dùng `type: "PROBE"`, `recipients` rỗng, TTL ngắn.

Document `advisories`: `ts`, `event_id`, `vid`, `action`, `t0`, `t1`, `t3`, `t4`. t5 do sim-bridge ghi vào metric và log Datadog.

Rollup: theo `edge` và phút, bỏ edge nội bộ của nút giao (tên bắt đầu bằng `:`), `$merge` với `whenMatched: "merge"` để rollup RSU không ghi đè rollup telemetry.

## Thời gian và đo vòng phản hồi

- `ts` là giờ giả lập, dùng cho luật và hàm cửa sổ. `t0` là wall clock (`time.time_ns()`) lúc sim-bridge đọc, chỉ dùng để đo độ trễ.
- t1 gateway nhận, t3 luật bắn (đã chọn xe nhận), t4 worker nhận qua change stream, t5 sim-bridge áp lệnh lên xe. t2 (ack của telemetry) đo riêng, không nằm trên đường sự cố.
- t0 và t5 cùng do sim-bridge ghi nên không lệch đồng hồ. Các mốc giữa chỉ tin được khi mọi service chạy chung một máy.
- `hanlane.loop.latency_ms = t5 - t0`, đẩy từ sim-bridge, tag `event_type` (gồm `probe`). `hanlane.detect.lag_ms` báo riêng độ trễ phát hiện.
- SUMO chạy theo thời gian thực với `--step-length 0.2`, telemetry 1 Hz. Bước 1 giây làm riêng việc chờ áp lệnh tốn tới 1 giây.
- Ngân sách: t0→t1 50 ms, t1→t3 30 ms, t3→t4 400 ms, t4→gửi lệnh 100 ms, gửi lệnh→t5 200 ms, tổng 780 ms. Nếu t3→t4 vượt, bật đường nhanh: gateway gọi thẳng worker, vẫn ghi `events`.
- Probe mỗi giây đi trọn vòng nhưng không ra lệnh cho xe nào, để p95, monitor, SLO luôn có dữ liệu.

## Luật phát hiện

Giá trị khởi đầu, để trong config, không hard-code. Mục tiêu dưới 1 giây tính từ bản ghi kích hoạt.

- vType khai báo rõ: mặc định SUMO cho ô tô decel 4,5 m/s², xe máy 10 m/s² (quá gắt). Ví dụ xe máy decel 4,5, emergencyDecel 8.
- HARD_BRAKE: `accel` dưới -6 m/s², ngưỡng theo loại xe.
- STALLED: dừng và bật đèn khẩn cấp thì bắn ngay. Không bật đèn: `speed` dưới 0,5 m/s quá 10 giây, là xe đầu hàng (không có xe dừng phía trước trong 30 m), và không đang chờ đèn đỏ (`nextTls`).
- LANE_JAM: tốc độ trung bình của làn dưới 30% giới hạn trong 60 giây, tính từ telemetry; RSU nếu kịp.
- Sự cố có vòng đời: mở khi phát hiện, đóng khi xe chạy lại; không bắn lại mỗi 30 giây.

Chọn xe nhận (trong gateway, từ RAM và đồ thị đường `sumolib`): xe trên cùng edge có `pos` nhỏ hơn xe gặp sự cố, hoặc trên các edge dẫn vào, trong 150 m; bỏ xe bám sát cách dưới 2 giây. HARD_BRAKE: `slowDown` (có thời lượng). STALLED: thêm `changeLane` nếu làn kế bên trống, kiểm tra `couldChangeLane` lúc áp.

## Giả lập

- Loại phương tiện: xe máy (vClass `motorcycle`), ô tô (`passenger`), xe buýt (`bus`). Tỉ lệ khởi đầu 85/12/3 (xe máy chiếm khoảng 92% phương tiện ở Đà Nẵng, số liệu 2018).
- Sublane `--lateral-resolution 0.8` bật từ đầu (tham số lúc khởi động). Chế độ "giao thông kiểu Hà Nội" (cắt đầu tiên nếu trễ) đổi tham số ngang của vType qua TraCI (`latAlignment`, `minGapLat`) và thêm xe máy.
- Làn lấy từ SUMO; ngoài đời cần định vị cấp làn hoặc camera trên xe. Đây là giả định, nói rõ khi trình bày.
- RSU: detector E1, E2 ở các nút giao chính, kèm pha đèn.
- GPS cộng nhiễu vài mét.
- Không theo kịp thời gian thực ở bước 0,2 s: giảm số xe, tắt sublane, hoặc bước 0,5 s và cập nhật ngân sách.
- `incident.py --edge <edge_id>`: dùng `setSpeedMode` cho xe phanh tới mức khẩn cấp, dừng hẳn, bật đèn khẩn cấp; mặc định cầu Rồng. `incident.py --clear` cho xe chạy lại.
- Lúc demo `--time-to-teleport -1`.
- Xe nhận cảnh báo đổi màu bằng `setColor`.
- `ab_run.py`: chạy cùng sự cố hai lần cùng seed, tắt và bật cảnh báo; so số lần phanh gấp và khoảng cách nhỏ nhất của các xe phía sau.
- Đổi mã xe theo từng chuyến (quyền riêng tư).

## Datadog

- Service APM: `hanlane-sim`, `hanlane-gateway`, `hanlane-worker`. Web là RUM nếu kịp, không phải APM. `DD_ENV=hackathon`, `DD_VERSION` = git sha.
- Metric (prefix `hanlane.`): `loop.latency_ms`, `detect.lag_ms`, `ingest.lag_ms`, `telemetry.received`, `events.detected`, `vehicles.active`, `mongo.batch_size`, `advisory.sent`.
- Tag: `zone`, `vehicle_type`, `event_type`, `action`. Không tag theo `vid`.
- Trace xuyên change stream: inject context bằng `HTTPPropagator` vào field `trace` của document `events`; worker extract rồi tạo span con, thêm một span lùi giờ bắt đầu về t3 để thấy khoảng chờ change stream.
- Log JSON, `DD_LOGS_INJECTION=true`. Log sự kiện kèm `latitude`, `longitude`.
- Dashboard, monitor, SLO viết JSON trong `datadog/`, đẩy bằng `scripts/dd_apply.py` (datadog-api-client). Không sửa tay trên UI rồi để lệch với repo.
- SLO: time-slice, p95 của `loop.latency_ms` dưới 1 giây trong từng phút, đạt ở 99% số phút.
- Monitor: p95 `loop.latency_ms` trên 800 ms trong 2 phút; p95 span pymongo của gateway trên 300 ms trong 1 phút; `telemetry.received` giảm hơn 50% so với 5 phút trước (change alert); no-data trên request của gateway trong 1 phút; tỉ lệ lỗi APM trên 1%; `events.detected{event_type:stalled}` tăng đột ngột theo khu.

## Biến môi trường

```
MONGODB_URI=
DD_API_KEY=
DD_APP_KEY=
DD_SITE=ap1.datadoghq.com
DD_ENV=hackathon
MAP_STYLE_URL=
MAP_API_KEY=
GATEWAY_URL=http://localhost:8000
SIM_CONTROL_URL=http://localhost:8100   # trong container: http://host.docker.internal:8100
WRITE_MODE=batch
```

## Mốc

- Ngày 1: kịch bản SUMO chạy được (sửa nút giao, pha đèn sau khi chuyển OSM); dữ liệu chảy vào Atlas; service map có MongoDB; percentile đã bật; `measure_m0.py` cho số đo để chốt số xe, TTL và xác nhận cách đếm thao tác.
- Ngày 2: luật, chọn xe nhận, `events`, change stream, worker, hàng đợi lệnh trong sim-bridge; probe; custom metric, trace xuyên vòng; so độ trễ từng đoạn với ngân sách.
- Ngày 3: bản đồ live; đường điều khiển nút bấm web → gateway → sim-bridge; chạy được kịch bản demo chỉ bằng trình duyệt.
- Ngày 4: dashboard, monitor, SLO dạng JSON; A/B; thử tải gấp 3 số xe và `WRITE_MODE=single`, quay lại để chiếu; rollup bằng `$merge`; bảng số đo.
- Ngày 5: 12 giờ trưa dừng thêm tính năng; chỉ sửa lỗi, README, slide, video dự phòng.

Thứ tự cắt nếu trễ: chế độ Hà Nội, tua lại, RSU cho LANE_JAM, cảnh Datadog MCP, key bản đồ thứ hai. Không cắt: vòng sự cố, probe, trace xuyên vòng, A/B.

## Không làm

Mô hình AI lái xe, nhận diện hình ảnh, app di động, đăng nhập người dùng.
