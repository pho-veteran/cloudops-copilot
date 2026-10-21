# Review độc lập: CloudOps Copilot release proposal (round 1)

## 1. Kết luận: CHANGES_REQUESTED

Hướng sản phẩm đúng. Incident workspace nối evidence với quyết định và kết quả, chỉ có ba action ghi, plan builder tất định và lifecycle tách riêng. Mức hardening cũng phần lớn vừa phải. Tuy vậy còn ba lỗi nhất quán ở mức thiết kế cần sửa trước khi chốt: nguồn gốc evidence/citation, luồng known-good cho action restore, và quy tắc cắt scope khi bị giới hạn bởi ngày 30/11. Các lỗi này không cần kết quả runtime mới sửa được, chỉ cần sửa tài liệu.

## 2. Findings theo mức độ

### Blockers

**B1. Evidence có hai đường thu, nhưng citation chỉ kiểm được một đường.** `CLOUDOPS_COPILOT_RELEASE_PROPOSAL.md:97-112`, `:150-152`, `:189`

- Trigger: trong sơ đồ, Kubernetes read chỉ đi qua `AK → GW → READ`. Workflow worker chỉ gọi Prometheus. Mục 6 thì yêu cầu thu status, logs, events, revision thành snapshot bất biến có ID, và citation validator kiểm tra ID/scope/time. Dòng 189 nhắc đến "collector" nhưng collector không có trong sơ đồ.
- Impact: dữ liệu agent đọc trực tiếp qua MCP Gateway không nằm trong evidence store của CloudOps. Vì vậy hoặc không cite được, hoặc phải dựa vào log upstream. Đây là gốc của typed RCA contract, Compare runs và việc plan "trỏ đúng run/evidence" (`:167`). Nếu chỗ này lệch thì cả ba đều lệch.
- Smallest fix: thêm "Evidence collector" vào sơ đồ, dùng read credential riêng. CloudOps tự thu toàn bộ snapshot (K8s và Prometheus) khi tạo run. Investigator chỉ được cite ID lấy từ tool GET evidence của CloudOps. Kubernetes read trực tiếp qua gateway hoặc tắt, hoặc ghi rõ là ngữ cảnh không cite được.

**B2. Known-good là điều kiện tiên quyết của restore nhưng không có journey.** `:85`, `:175`, `:207-218`, `:261`

- Trigger: "operator đánh dấu một template đang healthy". Không có endpoint, màn hình hay bước nào trong UI spec cho việc này. Quản trị config lại "qua CLI".
- Impact: scenario CrashLoop sau thay đổi chỉ chạy được nếu trước đó đã có người đánh dấu. Điều này mâu thuẫn với `:261` ("không cần sửa database/CLI để hoàn tất incident thông thường"). Trên thực tế restore thường trả về manual-only.
- Smallest fix: CloudOps tự ghi nhận ứng viên known-good khi một rollout đạt Available và ổn định trong N phút (lưu UID, digest, config hash). Tab Workload detail có nút "Confirm known-good" cho operator, kèm API tương ứng. Nếu vẫn chọn làm qua CLI thì phải ghi đây là bước setup bắt buộc trong scenario.

**B3. Chưa có quy tắc khi ngày và scope xung đột.** `:265-277`

- Trigger: hôm nay là 21/10, còn khoảng 6 tuần cho một developer. Khối lượng gồm SPA 9 route, FastAPI cùng worker/outbox, patch Akmatori với gate qua pi SDK, executor 3 template, KRR, retrieval cộng embedding, 15 lượt live, ablation, backup/restore, OpenTofu và video. Dòng 275 lại hạ 30/11 thành "mốc dự kiến của proposal trước", và chỉ cho cắt phần polish.
- Impact: constraint của người dùng là target 30/11. Tài liệu không nói gì thắng khi thiếu thời gian, nên kết cục dễ là trễ ngầm hoặc release thiếu luồng.
- Smallest fix: ghi rõ 30/11 là ràng buộc và đặt một mốc quyết định (ví dụ 4/11, sau capability gates). Cắt theo thứ tự được ghi sẵn: (1) semantic embedding và ablation, (2) Compare runs, (3) scale_deployment, (4) restore_deployment_template, (5) KRR. Giữ nguyên lõi: intake alert/manual, evidence, findings, OOM action, lifecycle, handoff. Ghi cụ thể rằng bản release khi đó vẫn đủ luồng cho các nhóm còn lại, và action bị cắt thì trả về manual-only.

### High

**H1. Tiêu chí verification lẫn với evaluation harness.** `:80-81`

"Không OOM mới dưới tải kiểm chứng" và "latency/error cải thiện dưới tải tương đương" chỉ có nghĩa trong scenario có load generator. Trong vận hành thật, post-check không biết "tải tương đương" là gì. Fix: post-check của sản phẩm gồm rollout Ready, không restart/OOM trong verification window đã cấu hình, và ngưỡng metric version hóa trong config. Tải tương đương chỉ là điều kiện của bộ đánh giá ở mục 12.

**H2. Chưa có quy tắc map alert sang workload và chính sách tự chạy analysis.** `:19`, `:144`, `:210`

Thiếu ba điều: alert không có label namespace/deployment hoặc nằm ngoài allowlist thì xử lý ra sao; mỗi alert có tự gọi LLM không; dedup key là gì. Với Free Tier và "một analysis active", một đợt alert storm sẽ làm đầy hàng đợi. Fix: alert ngoài scope chỉ ghi intake event và hiện trong Connections/health. Mỗi incident tự thu evidence và tự chạy tối đa một analysis. Dedup theo fingerprint của Alertmanager kết hợp workload.

**H3. "Run scope riêng" cho tool GET chưa khả thi với connector hiện tại.** `:220`, `:128`

HTTP connector dùng credential theo tool instance (`sources/http-executor.go.txt:11`, `Credentials`). Run ID vì vậy là tham số do model điền, nên model có thể hỏi evidence của run khác. Excerpt `incident-handler.go.txt:28-43` cho thấy `context` được merge vào DB còn agent chỉ nhận `req.Task`. Proposal thận trọng đúng ở điểm này. Fix: đưa run ID và scope vào `task`. Server kiểm tra run_id thuộc một run đang active. Chấp nhận token có phạm vi instance-wide (chỉ đọc, cùng cluster), hoặc ghi rõ cần patch forward incident UUID. Không hứa run scope chặt.

**H4. Rollback chưa rõ ai kích hoạt.** `:187`, UI spec `:146`

Rollback là một thao tác ghi. Tài liệu chưa nói nó tự chạy khi verification fail hay operator bấm. Nếu operator bấm thì cần confirmation hay approval mới không. Fix: một câu, ví dụ "rollback do operator kích hoạt với confirmation, kiểm tra lại state/UID, có audit; không tự động". Phương án tự động cũng được nhưng phải ghi vào policy của template.

### Medium

- **M1.** Proposal `:9` và UI spec `:3` gọi `docs/cloudops-ui-template.html` là "một phần của proposal", nhưng file này không có trong manifest. Sửa thành "artifact riêng, chưa nằm trong round này".
- **M2.** Acceptance cho negative và diagnostic cases chưa có ngưỡng (`:249-255`). Cần bổ sung: 5/5 negative không tạo executable plan; case log chứa chỉ dẫn không đáng tin không làm đổi template/params; Pending và ImagePull luôn ra manual-only.
- **M3.** Runbook được sync sang cả "runtime/index" (`:154`), trong khi sơ đồ cho agent dùng search của CloudOps. Chỉ nên giữ một index ở CloudOps để tránh hai catalog.
- **M4.** Research `:93` coi "threshold detection là release requirement", còn proposal không nhắc. Cần ghi rõ detection là Alertmanager rules đi kèm trong `deploy/`.
- **M5.** Window KRR 6–24 giờ liên tục (`:199`) ăn một phần lớn ngân sách compute 40–60 giờ (`:237`). Nên mặc định 6 giờ và ghi rõ giới hạn tính đại diện.

### Low / preference

- `ActionPlan` (`:164`) gộp cả state của execution, trong khi `:205` có thực thể Execution riêng. Nên tách để không có hai nguồn trạng thái.
- Filter "Blocked" (UI `:58`) chưa định nghĩa được suy ra từ đâu.
- Nên tắt namespace tool `proposals` của upstream. Đây là namespace credentialless (`authorizer.go.txt:23-26`), nên agent có thể tạo proposal runbook/skill trong DB upstream.
- Alert resolved nên hiện tín hiệu "có thể resolve" để incident không tồn đọng.

## 3. Tranh luận sản phẩm

**Phản biện mạnh nhất: kiến trúc agent.** Sau khi sửa B1, CloudOps đã tự thu toàn bộ evidence. Phần còn lại của Akmatori chỉ là vòng lặp agent và provider plumbing, đổi lại phải mang Go API, TS worker/pi SDK, MCP Gateway, một upstream patch, gate tương thích pi SDK và rủi ro allowlist mặc định allow-all (`authorizer.go.txt:110-114`). Một lời gọi Gemini structured output trực tiếp từ Python, nhận evidence bundle cộng các chunk runbook, tạo ra đúng typed contract ở `:136` với ít hơn 3 service và không cần patch. Ablation ở `:257` có so "evidence-only Investigator" nhưng vẫn chạy qua Akmatori, nên chưa trả lời được câu hỏi này.

**Khuyến nghị:** giữ yêu cầu tái sử dụng runtime vì người dùng đã chọn, nhưng ghi phần "justified" thành điều kiện. Capability gate 12.1 có hạn chót. Nếu đến hạn chưa đạt typed result qua pinned worker thì chuyển sang analyzer gọi trực tiếp trong CloudOps với cùng contract. Plan builder, executor và UI không đổi vì chúng chỉ phụ thuộc RCAResult. Nhờ vậy rủi ro lịch nằm ở một chỗ có thể thay được.

**UX.** Bốn khu vực cộng một workspace là đúng. Bỏ trang Approvals và Overview riêng là quyết định tốt, tôi đồng ý. Tôi phản đối hai điểm:

- Mobile từ 320px với luồng approve đầy đủ (UI `:34`) tốn công cho một app nội bộ chạy trên lab. Đề xuất desktop/tablet là chuẩn, mobile chỉ đọc.
- Compare runs là tính năng đẹp nhưng không nằm trên đường chính. Nó nên là mục cắt thứ hai, như B3.

**Scope.** Năm nhóm chẩn đoán, ba action là hợp lý về sản phẩm. Phản biện ở đây là ba action với một developer quá nhiều. OOM → set_memory_profile là golden flow. scale_deployment cần metric latency/RPS ở tầng ứng dụng, đây là giả định workload lab được instrument và tài liệu chưa nêu. Tôi giữ cả ba trong định hướng, nhưng thứ tự cắt phải có sẵn.

## 4. Độ phủ acceptance và overengineering

**Đã phủ tốt:** approval theo hash/TTL/idempotency, conditional write, write-uncertain/reconcile, no-op không tính là thành công, tách lifecycle, chặn release khi có lỗi ghi sai scope, không suy P95 từ 15 mẫu, backup/restore, các walkthrough UI (UI `:159`).

**Còn thiếu:** ngưỡng cho negative/diagnostic (M2), luồng known-good (B2), alert ngoài scope (H2), acceptance cho rollback do operator kích hoạt (H4).

**Overengineering có thể bỏ khỏi release:**

- Semantic embedding cùng đặc tả 768 chiều, format và cache (`:156`). Với vài chục runbook, full-text là đủ. Nên chuyển sang mục hoãn.
- Ablation ba nhánh (`:257`). Giữ rule baseline so với Investigator là đủ.
- Mobile approval ở 320px.
- Allocation cost estimate (`:201`). Chỉ hiện tài nguyên là đủ cho release.

Phần hardening (CSRF, cookie, RBAC tách writer) là mức tối thiểu đúng, không thừa.

**Câu hỏi còn mở:**

1. 30/11 là ràng buộc cứng hay mềm?
2. Workload lab có expose latency/RPS không?
3. Upstream result endpoint có trả `response` trước khi formatter xử lý không? Tôi chỉ thấy excerpt của worker `result-worker.ts.txt:38-52`, chưa thấy formatter.
4. Reviewer có cần role viewer riêng không, hay chỉ cần operator?

## 5. Giới hạn bằng chứng

- Chỉ đọc 3 file Markdown, manifest và 6 excerpt source. Không kiểm hash manifest vì không được chạy lệnh.
- Không có excerpt cho `response_formatter.go`, k8s connector, runbook service, KRR, tài liệu Gemini pricing/rate limits/embeddings, K3s và EC2. Các claim tương ứng tôi chưa xác minh, kể cả việc model `gemini-3.8-flash` và `gemini-embedding-2` tồn tại và có Free Tier.
- Prototype tương tác `docs/cloudops-ui-template.html` không được đánh giá vì không có trong candidate.
- Mọi capability gate (provider qua pi SDK, typed result, tài nguyên 8 GiB, quota) vẫn là bằng chứng implementation chưa có. Tôi không tính chúng là lỗi của proposal.
