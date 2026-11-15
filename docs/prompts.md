# Prompt theo ngày

Dán từng đoạn vào Claude Code khi bắt đầu mốc tương ứng. Việc lớn thì bật plan mode trước.

## Khởi động (ngày 0)

Đọc CLAUDE.md. Đây là repo hackathon 5 ngày. Dựng khung repo theo cấu trúc trong đó, docker-compose có datadog-agent, và db/init.py tạo collection, index cho MongoDB. Lên plan trước, chờ tôi duyệt.

## Ngày 1

Làm mốc ngày 1: SUMO bước 0,2 s; sim-bridge dùng subscription của TraCI, mỗi giây gom trạng thái xe, đổi tọa độ bằng sumolib, ts là giờ giả lập và t0 là giờ thật; gateway ghi ngay mỗi batch bằng insertMany. Viết scripts/measure_m0.py đo dung lượng, tỉ lệ nén, byte vào, và so 150 insert lẻ mỗi giây với một insertMany 300 bản ghi. Chạy thử rồi báo số đo.

## Ngày 2

Thêm luật HARD_BRAKE, STALLED, LANE_JAM đúng như CLAUDE.md. Khi luật bắn, gateway chọn xe phía sau từ RAM và đồ thị sumolib, ghi events ngay kèm danh sách xe và trace context; worker chỉ nghe insert trên change stream, gửi lệnh về sim-bridge qua hàng đợi, áp ở bước SUMO kế tiếp. Thêm probe mỗi giây. Đo t0 đến t5, đẩy hanlane.loop.latency_ms qua DogStatsD và so với ngân sách độ trễ.

## Ngày 3

Làm bản đồ live bằng MapLibre với bản đồ nền Việt Nam, nhận dữ liệu từ gateway qua WebSocket, không truy vấn Atlas. Thêm panel tổng quan và các nút: tạo sự cố trên cầu Rồng, dọn sự cố, ghi từng bản ghi. Nút bấm đi web tới gateway tới sim-bridge. Xong khi tôi chạy được cả kịch bản demo chỉ bằng trình duyệt.

## Ngày 4

Dùng Datadog MCP xem dashboard và monitor hiện có, viết lại thành JSON trong datadog/, thêm time-slice SLO p95 dưới 1 giây, bật include_percentiles. Sau đó chạy A/B tắt bật HanLane cùng seed, rồi bật WRITE_MODE=single trong 3 phút và cho tôi biết monitor nào bắn.

## Ngày 5

Từ 12 giờ trưa không thêm tính năng. Rà README cho người lạ chạy được từ đầu, liệt kê lỗi còn biết, và kiểm tra lại kịch bản demo 6 phút từ đầu tới cuối.
