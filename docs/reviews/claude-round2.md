# Review độc lập CloudOps Copilot, round 2

## 1. Kết luận: CHANGES_REQUESTED (mức nhẹ, chỉ cần sửa văn bản)

Hướng tính năng đã đủ để chốt. Ba blocker của round 1 đều đã được xử lý trong candidate:

- B1: collector đã có trong sơ đồ và tắt direct K8s read.
- B2: đã có journey và API cho Confirm known-good.
- B3: đã có checkpoint 04/11, thứ tự giảm công và nguyên tắc báo lên người dùng.

Còn một chỗ chưa xong, và đó là chính finding High H1 của round 1. Bảng hợp đồng action template và copy trong prototype vẫn dùng "tải tương đương / comparable load" làm điều kiện phục hồi, trái với định nghĩa post-check ở `:89`. Sửa khoảng 5 dòng là xong, không cần thêm bằng chứng runtime. Các điểm còn lại không chặn.

## 2. Findings chưa giải quyết (xếp theo mức độ)

**Medium (cần sửa trước khi chốt)**

**F1. Tiêu chí post-check vẫn mâu thuẫn (H1 mới sửa một phần).**
- Vị trí: `CLOUDOPS_COPILOT_RELEASE_PROPOSAL.md:80` ("không OOM mới dưới tải kiểm chứng") và `:81` ("latency/error cải thiện dưới tải tương đương"). Hai dòng này mâu thuẫn với `:89` ("'Tải tương đương' là điều kiện kiểm soát của benchmark").
- Prototype lặp lại lỗi này trong copy cho người dùng: `docs/cloudops-ui-template.html:40` ("post-check requires fresh measurements under comparable load"), `:48` và `:50` ("Recovery requires fresh telemetry, comparable load...").
- Hệ quả: bảng 3.2 là hợp đồng mà implementer sẽ đưa vào config template. Nếu làm theo bảng, gần như mọi action trong vận hành thật sẽ ra inconclusive, vì app không biết thế nào là "tải tương đương". Copy React cũng sẽ kế thừa câu sai.
- Fix nhỏ nhất: sửa `:80-81` thành "rollout Ready, không OOM/restart mới trong verification window, đạt ngưỡng metric version hóa và coverage tối thiểu". Đổi copy `:40/:48/:50` thành "fresh telemetry, configured thresholds and adequate coverage; falling traffic or missing coverage → inconclusive".

**Medium (không chặn)**

**F2. Spike Akmatori chưa có ngày bắt đầu và tiêu chí thoát khách quan.**
- Vị trí: `:131` dùng chữ "thay đổi rộng hoặc chưa đạt", `:263` liệt kê gate, còn `:297` lại để việc chuyển sang direct adapter thành một bước giảm công tại 04/11.
- Hệ quả: hai patch upstream (typed result ở `:141`, forward trusted incident UUID ở `:143`) cộng gate pi SDK có thể kéo dài ngầm đến 04/11. Khi đó spike thực tế không còn là hai ngày.
- Fix: ghi ngày kết thúc spike (trước khi làm màn phụ), và quy định chỉ cần một trong ba mục fail là chuyển adapter: typed result qua pinned worker, identity binding, Gemini qua pi SDK.

**F3. Restore phụ thuộc baseline đã được xác nhận từ trước, nhưng scenario và UI chưa nói rõ.**
- Vị trí: `:85-87` yêu cầu workload đang healthy mới confirm được. Workload đang CrashLoop thì không confirm được. Acceptance `:267`, `:271` không ghi bước setup này. Prototype `:58` thì cho sẵn baseline "Confirmed ... at 10:20".
- Hệ quả: người dùng mới gặp CrashLoop sẽ nhận manual-only mà không biết lý do. Bài reproduce của reviewer có thể fail vì thiếu bước này.
- Fix: thêm vào scenario restore "confirm known-good qua Workload detail trước khi inject". Next step hiện lý do "No confirmed known-good baseline" và link sang Workload detail.

**Low**

- **F4.** `:159` có trạng thái "analysis deferred" nhưng enum InvestigationRun `:178` không có. Blocked filter (UI `:134`) cũng không tính trạng thái này. Fix: thêm `deferred`, hoặc ghi rõ nó là `queued` kèm reason.
- **F5.** Rollback `:203` "kiểm tra lại TTL": không rõ là TTL của plan gốc hay một cửa sổ riêng cho rollback. Nếu dùng TTL gốc, rollback có thể hết hạn trong lúc verifying. Fix: rollback có eligibility window riêng, tính từ lúc verification kết thúc.
- **F6.** Điều kiện restore `:79` ghi "trường liên quan chưa đổi". Sau một thay đổi xấu thì trường đã đổi so với known-good. Nên ghi rõ là "chưa đổi kể từ lúc lập plan".
- **F7.** Phạm vi ablation chưa nhất quán: `:277` và research `:139` vẫn là ba nhánh, còn `:297` nói bỏ "ablation mở rộng". Cần ghi bản tối thiểu là rules so với Investigator, nhánh retrieval chỉ chạy khi bật semantic.
- **F8.** Prototype lệch enum ở vài chỗ:
  - Resolve đặt plan thành `stale` thay vì `invalidated` (`:98`).
  - Execution `failed` hiển thị thành "Manual intervention" (`:22`, `:45`).
  - Queue chỉ có filter all/approval/manual (`:31`), trong khi spec có 5 nhóm (UI `:58`).

  Đây là giới hạn của bản mô phỏng, không gây hiểu nhầm về an toàn. Nên ghi chú để bản React không sao chép theo.

## 3. Đối chiếu từng finding round 1

| Round 1 | Trạng thái | Bằng chứng trong candidate |
|---|---|---|
| B1 evidence/citation | Đã xử lý | Sơ đồ `:103-109` có COL→READ/PROM/DB, không còn GW→READ; `:125` tắt direct K8s tool và chỉ cite snapshot đã đăng ký |
| B2 known-good | Đã xử lý (dư F3) | `:87`, API `:231`, UI spec `:76`, prototype `:58`, `:81` |
| B3 ngày và scope | Đã xử lý, author từ chối một phần có lý | `:295-297`: checkpoint 04/11, thứ tự giảm công, không tự cắt 3 action/KRR, báo người dùng quyết định |
| H1 post-check | Một phần | `:89` đúng, nhưng `:80-81` và prototype `:40/:48/:50` còn sai (F1) |
| H2 alert mapping/dedup | Đã xử lý (dư F4) | `:159` |
| H3 run scope | Đã xử lý | `:143` (patch forward UUID là gate, thiếu binding thì từ chối), `:236`; fallback ở `:131` |
| H4 rollback | Đã xử lý (dư F5) | `:203`, UI spec `:118` |
| M1 prototype trong manifest | Đã xử lý | `manifest.json:15` |
| M2 negative/diagnostic | Đã xử lý | `:275` (4/4, 5/5, quy tắc cho injection) |
| M3 một index | Đã xử lý | `:169` |
| M4 threshold rules | Đã xử lý | `:159` |
| M5 KRR 6h | Đã xử lý | `:215` |
| Low: tách Plan/Execution | Đã xử lý | `:179-180` |
| Low: Blocked filter | Đã xử lý | UI spec `:134` |
| Low: tắt proposals | Đã xử lý | `:143` (cả memory/cron) |
| Low: alert resolved | Đã xử lý | UI spec `:134`, `:177` |
| Mobile chỉ đọc | Author từ chối, tôi chấp nhận | Layout responsive dùng chung không tốn thêm nhiều. Viết chính sách riêng theo thiết bị lại thêm logic. Prototype đã được QA ở 320px (`verification-round2.md:9`) |
| Compare là mục cắt | Một phần, chấp nhận | Giữ ở mức hai snapshot lưu sẵn, không gọi AI (`:50`, `:297`) |
| Overengineering: embedding, money estimate | Đã xử lý | Optional ở `:171`, `:217`, `:297` |

## 4. Phản biện mạnh nhất và khuyến nghị

**Phản biện.** Sau B1, agent không còn đọc cluster trực tiếp. Tool duy nhất của nó là GET evidence mà CloudOps đã thu sẵn. Để giữ Akmatori, dự án phải làm hai patch upstream (typed result và forward identity qua gateway), qua gate pi SDK, và vận hành thêm Go API, TS worker cùng MCP Gateway. Đổi lại chỉ được một vòng lặp đọc lại dữ liệu đã có trong tay. Interface `Investigator.analyze(run, evidence, knowledge)` ở `:131` đã nhận evidence làm tham số. Nghĩa là direct adapter thỏa contract tự nhiên hơn và không cần binding scope. Về kỹ thuật, direct adapter nên là mặc định.

**Phản hồi của author.** Author coi runtime là phần thay được, đặt timebox hai ngày và chấp nhận fallback. Tôi đồng ý đây là một tradeoff về lịch hợp lệ, không phải yêu cầu mâu thuẫn. Akmatori vẫn được ưu tiên trong các quyết định trước, và chi phí sai đã được khoanh vào một interface.

**Khuyến nghị.** Giữ cách làm của author, nhưng sửa F2: spike có ngày kết thúc cố định, ba tiêu chí pass/fail rõ ràng, và fail một mục là chuyển sang direct adapter ngay, không đợi checkpoint 04/11.

Về ngày 30/11: việc không cắt sẵn 3 action/KRR và chỉ báo lên người dùng khi thiếu thời gian là đúng với ràng buộc "không âm thầm bỏ chức năng". Mốc 04/11 nên tạo ra một bản ước lượng viết theo từng chặng ở `:287-293`. Như vậy "dự báo ngày mới" ở `:297` có cơ sở, chứ không chỉ là một tuyên bố.

## 5. Follow-up không chặn và giới hạn kiểm chứng

**Follow-up**
- F2–F8 ở trên.
- Prototype chưa mô phỏng rollback eligible, uncertain write hay no-op. Spec (UI `:118`, `:152-154`) và acceptance (`:270`, `:275`) đã bao phủ các trường hợp này, nên chấp nhận được.
- Bảng stack `:244` và Connections trong prototype (`:60`) nên nhắc rằng có thể thay bằng direct adapter.
- Research `:97` có thể ghi rõ threshold rules nằm trong `deploy/`.

**Giới hạn**
- Tôi chỉ đọc tĩnh 3 file Markdown, file HTML, manifest và các file review/response/verification. Không chạy prototype và không kiểm hash manifest.
- Tôi không mở lại 6 excerpt trong `sources/`. Đánh giá H3 dựa trên excerpt đã đọc ở round 1 và văn bản hiện tại.
- Các kết quả QA và browser trong `verification-round2.md` là khẳng định của bên khác, tôi chưa tự kiểm chứng.
- Gate provider, pi SDK, quota, binding và tài nguyên 8 GiB đều là gate của giai đoạn implementation. Tôi không tính chúng là lỗi của plan và không suy ra là chúng đã đạt.
