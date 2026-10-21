# Review kiến trúc CloudOps Copilot, vòng 5

## 1. Kết luận: APPROVE

Tôi duyệt bản kế hoạch này. Không có lỗi P0 hay P1. Có năm điểm P2 nên đưa vào tài liệu trước khi bắt đầu chặng "Recovery hoàn chỉnh". Đây là những ràng buộc thiết kế để làm đúng, không phải lý do để bàn lại hướng kiến trúc.

Phạm vi tôi đã đọc: đủ năm file trong `manifest.json`. Template HTML tôi đọc chọn lọc qua grep và các dòng 50–57. Tôi đối chiếu source Aurora ở các file `workflow.py`, `safe_memory_saver.py`, `docker-compose.yaml`, `celery_config.py`, `background/task.py`, `infrastructure_confirmation.py` và `command_gate.py`. Tôi không tính SHA256, không chạy gì, không đọc các review của vòng trước.

## 2. Các phát hiện

**P2-1. Bản debate thiếu phương án ở giữa: thư viện hàng đợi chạy trên chính PostgreSQL**
- Vị trí: `CLOUDOPS_COPILOT_ARCHITECTURE_DECISION.md:56-61`, `CLOUDOPS_COPILOT_RELEASE_PROPOSAL.md:126`
- Trigger: bảng so sánh chỉ có Akmatori, Aurora, LangGraph và tự viết. Còn thiếu loại thư viện như Procrastinate hoặc DBOS Transact. Loại này không thêm service nào, vẫn dùng PostgreSQL làm nguồn trạng thái.
- Impact: phần dễ sai nhất của "engine tự chế" là claim, lease, fencing token, heartbeat, backoff và takeover. Phần này lại là phần được đặc tả ít nhất. Đây đúng là chỗ có nguy cơ "dời độ phức tạp sang engine tự viết".
- Fix nhỏ nhất: thêm một dòng vào bảng debate. Nếu vẫn tự viết thì chốt contract tối thiểu:
  - một bảng `jobs`, claim bằng `FOR UPDATE SKIP LOCKED`;
  - mọi lần ghi kết quả đều `WHERE lease_token = :t`;
  - output của từng stage có khóa idempotent `(run_id, stage, attempt)`.

**P2-2. Executor bị tiếp quản giữa lúc đang ghi chưa có quy tắc rõ**
- Vị trí: `CLOUDOPS_COPILOT_RELEASE_PROPOSAL.md:126`, `:198-200`
- Trigger: lease của executor hết hạn khi Execution đang ở `executing`.
- Impact: tài liệu chỉ nói "reconcile trước khi retry". Chưa có câu nào cấm executor mới chạy lại bước write. Kubernetes đã nhận patch hay chưa thì lúc đó không biết.
- Fix: thêm một câu: takeover ở `executing` luôn chuyển sang `uncertain`, rồi mới reconcile. Nên ghi thêm cách nhận biết kết quả khi reconcile:
  - so trường đích với giá trị mong muốn;
  - với restore hoặc memory, có thể đặt annotation `execution-id` trên Deployment trong cùng patch;
  - riêng scale qua subresource `/scale` thì không gắn được annotation, nên chỉ so được `replicas`.

**P2-3. Execution `uncertain` có thể kẹt vĩnh viễn**
- Vị trí: `CLOUDOPS_COPILOT_RELEASE_PROPOSAL.md:184`, `:186`, `CLOUDOPS_COPILOT_UI_UX_SPEC.md:155`
- Trigger: Kubernetes API không truy cập được kéo dài, nên reconcile không bao giờ có kết quả.
- Impact: resolve bị chặn khi còn `uncertain`. UI chỉ cho "chờ xác minh". Incident không đóng được nếu operator không sửa database, trái với `:281`.
- Fix: sau N lần reconcile thất bại, tự chuyển sang `manual_intervention`. Hoặc cho operator chuyển thủ công kèm reason, sau khi đã xem bản đọc target mới nhất. Lưu audit cho bước này.

**P2-4. Tham số giới hạn output không trung lập giữa các provider**
- Vị trí: `CLOUDOPS_COPILOT_RELEASE_PROPOSAL.md:150-152`
- Trigger: "output budget" bắt buộc phải gửi một tham số. Một số model OpenAI mới (nhất là reasoning) từ chối `max_tokens` và chỉ nhận `max_completion_tokens`, hoặc từ chối `temperature`. Endpoint khác lại có thể chỉ hiểu `max_tokens`.
- Impact: câu "không gửi tham số nâng cao theo giả định" vẫn bị vi phạm ngay ở tham số cơ bản nhất. Lỗi này có thể chặn đúng mục tiêu "bất kỳ model nào".
- Fix: tên tham số output-token là cấu hình, gồm `max_tokens`, `max_completion_tokens` hoặc không gửi. `temperature` mặc định không gửi. Thêm case này vào fixture "unsupported parameters" đã có ở `:154`.

**P2-5. Nhóm CrashLoop "sau thay đổi" bỏ sót thay đổi nằm ngoài pod template**
- Vị trí: `CLOUDOPS_COPILOT_RELEASE_PROPOSAL.md:67`, `:79`, `:166`
- Trigger: crash xảy ra do ConfigMap được tham chiếu bị sửa. Việc sửa này không tạo revision mới, nên template diff rỗng.
- Impact: hoặc RCA sai, vì "không đổi template" bị hiểu thành "không có thay đổi". Hoặc tạo plan restore có diff rỗng.
- Fix:
  - collection pack ghi metadata của ConfigMap/Secret được tham chiếu (tên, `resourceVersion` hoặc thời điểm sửa), tuyệt đối không đọc nội dung Secret;
  - planner thêm luật: diff rỗng thì trả manual-only;
  - liệt kê "thay đổi ngoài template" vào phần giới hạn của restore.

**P3, không chặn:**
- `CLOUDOPS_COPILOT_RELEASE_PROPOSAL.md:87` và `UI_UX_SPEC.md:78`: restore chỉ dùng được nếu operator đã confirm baseline từ trước. Nên hiển thị tình trạng baseline (có/không, tuổi, số revision kể từ đó) ở Workloads, và trong decision preview của restore. Có thể gợi ý "Confirm known-good" sau khi resolve thành công và đã qua cửa sổ ổn định.
- `CLOUDOPS_COPILOT_RELEASE_PROPOSAL.md:199`: nói rõ `resourceVersion` lấy từ lần đọc mới ngay trước khi ghi, không lấy từ lúc lập plan. Status update cũng làm tăng `resourceVersion`, nên dùng giá trị lúc lập plan thì gần như luôn stale.
- `CLOUDOPS_COPILOT_RELEASE_PROPOSAL.md:152`: khi context vượt budget, "dữ liệu quan trọng" chưa có định nghĩa. Nên để collection pack khai báo nguồn bắt buộc và nguồn tùy chọn. Nguồn bắt buộc không vừa budget thì trả `insufficient_evidence`.
- `CLOUDOPS_COPILOT_RELEASE_PROPOSAL.md:154`: yêu cầu "hai cấu hình endpoint/model" phải nằm trong chính sách chỉ dùng đường miễn phí. Nếu không có cấu hình miễn phí thứ hai thì chủ tài khoản quyết định, không tự đổi.
- `CLOUDOPS_COPILOT_RELEASE_PROPOSAL.md:244`: chốt dùng SDK `openai` Python với `base_url`, hoặc `httpx` thuần, để tránh tự viết client.
- `docs/cloudops-ui-template.html:52`: handoff Markdown thiếu hai mục "đã loại trừ" và "chưa kiểm tra", trong khi spec ở `UI_UX_SPEC.md:128` yêu cầu có. Resolve modal (`:55`) cũng chưa preview snapshot. Mục giới hạn demo ở `UI_UX_SPEC.md:165` không liệt kê thiếu sót này.
- `docs/cloudops-ui-template.html:26`: `window.openai?.setWidgetState` là đoạn sót lại từ host widget. Không nên mang sang bản React.

## 3. Debate: workflow trực tiếp hay runtime/thư viện

**Lập luận mạnh nhất cho việc giữ runtime hoặc thư viện:**
1. Collection pack cố định là canh bạc lớn nhất của thiết kế. Khi gặp sự cố ngoài playbook, agent có tool loop vẫn còn tự tìm thêm được. CloudOps thì chỉ trả về "missing evidence".
2. Lease, takeover và reconcile là phần thường sai ở lần viết đầu, và tài liệu mới đặc tả nó trong khoảng một đoạn văn.

Cả hai điểm đều có thật. Nhưng các runtime đang được cân nhắc không giải quyết điểm thứ hai. Theo source tôi đã đọc:
- Aurora lưu trạng thái graph trong RAM (`SafeMemorySaver` kế thừa `MemorySaver`).
- Confirmation nằm trong dict `_pending_confirmations` trong process, chờ bằng vòng polling có timeout.
- Celery task dùng `acks_late` và `reject_on_worker_lost`, broker là Redis với `allkeys-lru`, không thấy volume hay AOF trong Compose.
- Phần dedup action trong Aurora dùng advisory lock của Postgres ở `actions/executor.py`, không phải Celery.

Tức là kể cả khi dùng Aurora, CloudOps vẫn phải tự xây durable approval/execution. Akmatori cũng vậy: evidence, planner, executor và lifecycle vẫn là phần của CloudOps. Lợi ích duy nhất còn lại là tool loop, và thiết kế này cố ý không dùng nó.

**Có phải chỉ đang dời độ phức tạp sang engine tự chế không?** Một phần là có, nhưng hẹp hơn nhiều so với một agent framework:
- không có tool pairing, conversation state hay quirk của tool calling theo từng provider;
- phần thật sự tự viết chỉ là job queue cộng executor.

Rủi ro nằm ở job queue. Vì vậy P2-1 là điểm nên xử lý: dùng thư viện queue trên Postgres hoặc chốt contract tối thiểu. Không cần LangGraph hay Temporal.

**Aurora có bị mô tả công bằng không?** Có.
- Graph mặc định là một node (`ORCHESTRATOR_ENABLED:-false`), nhưng node đó chứa một tool loop phức tạp. Tài liệu ghi nhận đúng điều này.
- Safeguard được ghi nhận đúng: `command_gate` có nhiều lớp (signature, org allow/deny, LLM judge, session taint), và trong background chat thì bị chặn là từ chối luôn chứ không tự cho qua.
- Lời phê bình chỉ nhắm vào mô hình "confirmation trong chat" khác với "approval gắn plan hash". Điểm này chính xác.
- Chỉ có một chỗ nói hơi quá: dedup không thuộc Celery mà là advisory lock trên Postgres.

**Khuyến nghị:** giữ workflow trực tiếp bằng FastAPI và PostgreSQL. Bổ sung một trong hai:
- dùng một thư viện job chạy trên chính PostgreSQL (Procrastinate hoặc DBOS Transact, sau khi kiểm tra phù hợp);
- hoặc ghi contract tối thiểu ở P2-1, kèm test takeover.

Điều kiện mở lại quyết định ở `ARCHITECTURE_DECISION.md:67` là hợp lý. Nếu sau này cần khám phá thêm, chỉ nên thêm một vòng tool có giới hạn bên trong Investigator, chỉ gọi lại các collector có scope sẵn, không có credential.

## 4. Độ phủ tính năng, UI/UX và giới hạn kiểm chứng

**Đã phủ đủ:**
- intake từ alert và điều tra thủ công;
- Findings có tách đã loại trừ / chưa kiểm tra;
- evidence drawer, compare, decision preview;
- approve, reject, stale, expiry;
- execution với các outcome riêng;
- rollback có cửa sổ riêng;
- resolve/reopen với snapshot;
- KRR có exclusions;
- Knowledge, Connections, viewer/operator, session expiry, ma trận trạng thái;
- ba walkthrough bắt buộc ở `UI_UX_SPEC.md:169`.

Phạm vi khớp ý định của bạn: Vite, bốn khu vực, không clone Akmatori, không chat thực thi, không có enterprise hardening. Tôi không thấy scope creep. Rủi ro lịch 30/11 đã được thừa nhận ở `:295-297`.

**Còn thiếu so với các luồng UX:**
- đường thoát khỏi `uncertain` (P2-3);
- hiển thị tình trạng baseline;
- handoff trong template thiếu hai mục ruled-out/unverified;
- chưa có thông báo khi execution kết thúc lúc operator đã rời màn hình (chấp nhận được vì Slack đã hoãn và UI có polling).

**Những gì chưa được kiểm chứng:**
- Luồng snapshot lúc resolve chưa được walkthrough trên trình duyệt. Template cũng không lưu snapshot.
- Chưa gọi endpoint thật. Chưa test crash hay hết lease của job, chưa cài full stack, chưa đo recovery hay chất lượng RCA.
- Trong review này, tôi chỉ đọc source Aurora, không phải hành vi runtime đã đo. Tôi không kiểm tra lại các link GitHub ngoài bản sao trong `sources-aurora/`.
