# Nghiên cứu định hướng release CloudOps Copilot

Ngày đối chiếu nguồn: 21/10/2026. Phạm vi là release nội bộ cho một nhóm vận hành nhỏ, một Kubernetes cluster và các namespace được cấu hình. Mục tiêu là chốt tính năng có thể sử dụng trọn luồng, với mức kiểm soát phù hợp cho những thao tác ghi đã hỗ trợ.

**Quyết định kiến trúc mới nhất:** bỏ runtime Akmatori, không thay bằng Aurora; tự điều phối workflow hữu hạn trong CloudOps và gọi endpoint trực tiếp. [Báo cáo Aurora và debate kiến trúc](CLOUDOPS_COPILOT_ARCHITECTURE_DECISION.md) phân tích source Aurora tại `5af24c46416ed1bc4eec057d88db0af60b6ed3c6`, các điểm học/adapt/defer và lý do chọn. Các phần audit Akmatori dưới đây giữ làm bằng chứng cho phương án đã cân nhắc, không là integration requirements còn hiệu lực.

**Cập nhật theo quyết định người dùng:** contract inference là OpenAI-compatible endpoint với base URL/API key/model cấu hình, không khóa Gemini. Phần kiểm chứng Gemini dưới đây giữ vai trò case study nguồn, không là dependency bắt buộc. Direct adapter là client dùng contract chung; endpoint không có tool calling vẫn dùng được với evidence/runbook context chuẩn bị ở server và JSON được app validate. Embedding tách cấu hình, optional. Chi tiết hiện hành ở release proposal mục 5.3 và 6. Gemini cũng công bố [đường OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai).

## Kết luận định hướng

CloudOps Copilot nên là workspace điều tra sự cố Kubernetes và phục hồi có phê duyệt, kèm khuyến nghị tài nguyên. Người vận hành có thể bắt đầu từ alert hoặc một workload đang có vấn đề; xem bằng chứng, nhận kết luận có giới hạn rõ ràng, quyết định hành động và theo dõi kết quả. Một incident vẫn có giá trị khi chưa có hành động tự động phù hợp.

Frontend dùng React/Vite theo quyết định của người dùng. Giao diện và tính năng được thiết kế riêng theo công việc vận hành; Akmatori và Aurora là nguồn tham khảo, không là runtime dependency hay mẫu giao diện để sao chép.

Các quyết định đề xuất:

1. CloudOps tự quản lý workflow, jobs và một Investigator; gọi OpenAI-compatible endpoint trực tiếp. Bỏ Akmatori runtime và spike giữ/bỏ upstream. CloudOps sở hữu incident, evidence, action, approval và verification trong cùng domain model.
2. Bỏ yêu cầu hai agent RCA và Planner chạy tuần tự. Investigator đề xuất giả thuyết và candidate; backend tạo executable plan từ template đã định nghĩa.
3. Phân biệt phạm vi chẩn đoán với phạm vi ghi. Hỗ trợ năm nhóm triệu chứng phổ biến, nhưng chỉ ba action template trên Deployment.
4. Bổ sung điều tra thủ công, điều tra lại có so sánh, lifecycle incident độc lập, health và báo cáo bàn giao.
5. Runbook retrieval có citation là tính năng release. Embedding là phương án nâng chất lượng; full-text/metadata retrieval luôn hoạt động khi embedding không sẵn sàng.
6. Web là nơi duyệt hành động đầy đủ. Slack notification có deep link là mở rộng tiện ích; Slack approval chưa nằm trên đường bắt buộc để release.

## Phương pháp và mức độ bằng chứng

Đã đọc proposal hiện tại; ba nhánh nghiên cứu độc lập dùng GPT-6.1 Sol để kiểm tra Akmatori, Gemini và product scope. Mã Akmatori được đối chiếu tại commit `4b9e984fbfb6818d3e5798228af1010c1fc0ddc8`; KRR được kiểm tra thêm tại tag `v1.31.0`. Tài liệu provider được đọc ở phiên bản công khai hiện hành.

Đây là kiểm chứng tài liệu và source code. Chưa chạy inference, build toàn bộ stack, triển khai EC2 hoặc dùng credential tài khoản. Khả năng provider được tài liệu hỗ trợ, tính tương thích qua runtime và quota tài khoản là ba mức bằng chứng khác nhau. Các đề xuất UX và kiến trúc dưới đây là quyết định thiết kế, không phải kết quả đo hiệu quả người dùng.

## Bài toán và đóng góp của sản phẩm

Điểm khác biệt nên tập trung vào việc nối bằng chứng, quyết định và kết quả của một incident. Việc thêm một hộp chat hoặc một dashboard tổng hợp chưa giải quyết được câu hỏi người vận hành cần trả lời: chuyện gì đang xảy ra, bằng chứng nào hỗ trợ kết luận, điều gì sẽ thay đổi, và thay đổi đó có giúp workload phục hồi không.

Đóng góp tự phát triển gồm incident workspace, evidence snapshot và so sánh giữa các lần điều tra; typed RCA contract; workflow/job lifecycle; deterministic plan builder; approval/execution/verification; resource recommendation có dữ liệu nguồn; và scenario harness để kiểm chứng. Dùng thư viện/API client tiêu chuẩn; học collector/runbook/prompt patterns từ upstream. Chưa copy code/assets upstream; nếu reuse từng phần sau này phải ghi attribution và commit cụ thể.

## Kiểm chứng Akmatori — phương án đã cân nhắc

| Khả năng | Bằng chứng tại commit đã pin | Hệ quả cho proposal |
|---|---|---|
| Tạo investigation | Public request có `task` và `context`; trả UUID và chạy bất đồng bộ. [Types](https://github.com/akmatori/akmatori/blob/4b9e984fbfb6818d3e5798228af1010c1fc0ddc8/internal/api/types.go), [handler](https://github.com/akmatori/akmatori/blob/4b9e984fbfb6818d3e5798228af1010c1fc0ddc8/internal/handlers/api_incidents.go) | Không truyền role/model/schema tùy ý như thể API đã hỗ trợ. Essential context phải được adapter kiểm chứng là agent nhận được. |
| Phạm vi skill và tool | API dùng các skill đang bật và hợp tool allowlist của chúng. [Skill service](https://github.com/akmatori/akmatori/blob/4b9e984fbfb6818d3e5798228af1010c1fc0ddc8/internal/services/skill_service.go) | Một profile điều tra cố định giúp tránh phải xây role router. Dùng instance/config riêng chỉ bật các skill cần thiết. |
| Google provider | Runner có mapping Google, giải quyết model và gọi SDK session. [Runner](https://github.com/akmatori/akmatori/blob/4b9e984fbfb6818d3e5798228af1010c1fc0ddc8/agent-worker/src/agent-runner.ts), [packages](https://github.com/akmatori/akmatori/blob/4b9e984fbfb6818d3e5798228af1010c1fc0ddc8/agent-worker/package.json) | Có đường tích hợp source-level; cần khóa dependency thực tế và thử selected model qua đúng đường này. |
| Kết quả agent | Output hiện là assistant text; formatter có thể viết lại, response có footer metrics. [Formatter](https://github.com/akmatori/akmatori/blob/4b9e984fbfb6818d3e5798228af1010c1fc0ddc8/internal/services/response_formatter.go) | Bổ sung một kết quả có schema được lưu riêng trước formatting; không parse văn bản trình bày để tạo action. |
| Kubernetes connector | GET-only, có chặn tài nguyên/subresource nhạy cảm và cache read. [Connector](https://github.com/akmatori/akmatori/blob/4b9e984fbfb6818d3e5798228af1010c1fc0ddc8/mcp-gateway/internal/tools/k8s/k8s.go) | Phù hợp investigation; executor tách riêng. Verification phải đọc mới, không dùng cache điều tra làm bằng chứng phục hồi. |
| HTTP read tools | `read_only=true` chặn mọi method khác GET. [Executor](https://github.com/akmatori/akmatori/blob/4b9e984fbfb6818d3e5798228af1010c1fc0ddc8/mcp-gateway/internal/tools/httpconnector/executor.go) | Đổi evidence/runbook search thành GET với tham số giới hạn và xác thực; không chỉ gọi POST là “read-only” trong tài liệu. |
| Tool allowlist | Không có hoặc hết hạn allowlist có thể cho phép toàn bộ tool; empty list có nghĩa khác. [Authorizer](https://github.com/akmatori/akmatori/blob/4b9e984fbfb6818d3e5798228af1010c1fc0ddc8/mcp-gateway/internal/auth/authorizer.go) | Credential chỉ đọc và không có write connector là ranh giới bắt buộc. Không coi prompt/skill filter là sandbox. |
| Proposals có sẵn | Các loại proposal phục vụ runbook, memory, cron và skill prompt. [Models](https://github.com/akmatori/akmatori/blob/4b9e984fbfb6818d3e5798228af1010c1fc0ddc8/internal/database/models_proposals.go) | Không tái sử dụng nhầm thành hệ thống approve Kubernetes action. |
| Runbook | CRUD và đồng bộ Markdown; có tìm kiếm file. [Service](https://github.com/akmatori/akmatori/blob/4b9e984fbfb6818d3e5798228af1010c1fc0ddc8/internal/services/runbook_service.go), [searcher](https://github.com/akmatori/akmatori/blob/4b9e984fbfb6818d3e5798228af1010c1fc0ddc8/akmatori_data/agents/runbook-searcher.md) | Có baseline retrieval để so sánh trước khi bắt buộc pgvector. Cần một nguồn runbook chuẩn, tránh hai catalog độc lập. |
| Auth và UI | Upstream có React/Vite và các màn hình vận hành; auth dùng admin cấu hình/JWT, không chứng minh các role sản phẩm trong proposal. [Web](https://github.com/akmatori/akmatori/blob/4b9e984fbfb6818d3e5798228af1010c1fc0ddc8/web/package.json), [auth](https://github.com/akmatori/akmatori/blob/4b9e984fbfb6818d3e5798228af1010c1fc0ddc8/internal/middleware/jwt_auth.go) | Frontend Vite mới dùng identity của CloudOps. Upstream là dịch vụ nội bộ, credential quản trị không được chuyển ra browser. |

### Kết quả của việc cân nhắc tái sử dụng

Phương án trước dự định giữ worker/provider plumbing và thêm patch typed-result cùng trusted run binding. Sau khi đối chiếu scope và Aurora, lợi ích đó không đủ bù chi phí: CloudOps vẫn phải sở hữu evidence, jobs, lifecycle, planner và executor. Vì vậy bỏ runtime ngoài, các patch/gateway/mapping ID không còn là công việc release.

Runbook chuẩn nằm trong repository CloudOps và index một chiều tại CloudOps. Collector ghi Kubernetes/Prometheus thành snapshot; context builder lấy evidence/runbook cùng scope rồi gửi model. Chỉ học patterns và phương pháp điều tra, chưa import connector/runtime upstream. Các kết luận source trong bảng trên vẫn hữu ích để giải thích quyết định không tích hợp; cột hệ quả mô tả yêu cầu của phương án cũ.

## Kiểm chứng Gemini và retrieval

`gemini-3.8-flash` và `gemini-embedding-2` đều được tài liệu hiện hành liệt kê. Bảng giá có Free Tier cho Standard API của hai model; Batch embedding không thuộc đường miễn phí này. Cần dùng đúng project/tier, tắt fallback trả phí và chỉ gửi telemetry phù hợp với chính sách dữ liệu. [Models](https://ai.google.dev/gemini-api/docs/models), [pricing](https://ai.google.dev/gemini-api/docs/pricing).

Quota áp dụng theo project, tùy model/tier; giới hạn thực tế phải xem trong AI Studio. Vì vậy không thể chốt số incident mỗi ngày từ bảng giá công khai. App cần hiển thị hàng đợi, lần thử gần nhất, lỗi provider và khả năng retry; không hiển thị “quota còn lại” nếu không có nguồn số liệu. [Rate limits](https://ai.google.dev/gemini-api/docs/rate-limits).

Google hỗ trợ function calling và structured output, nhưng hỗ trợ ở provider chưa xác nhận đường SDK trong Akmatori đã chuyển tiếp đầy đủ schema, tool results và conversation state. Combined tools và structured output cần thử riêng. [Function calling](https://ai.google.dev/gemini-api/docs/function-calling), [structured output](https://ai.google.dev/gemini-api/docs/structured-output), [SDK](https://ai.google.dev/gemini-api/docs/libraries).

Embedding 2 hỗ trợ 768 chiều, tự normalize dimension rút gọn, không dùng `task_type`. Format retrieval là `task: search result | query: ...` cho query và `title: ... | text: ...` cho document. Nhiều contents có thể được gộp thành một embedding, nên mỗi chunk phải có request Standard độc lập hoặc phương thức đã kiểm chứng trả từng vector. Cache theo model, dimension, input format và hash. [Embeddings](https://ai.google.dev/gemini-api/docs/embeddings).

### Quyết định về agent và RAG

Một Investigator thu thập/đọc evidence, nêu giả thuyết, chỉ rõ điều còn thiếu và có thể đề xuất template ID. Plan builder xác định target, params, preconditions, diff, TTL và verification bằng code. Hai lần gọi cùng model không tạo ra một bên kiểm chứng độc lập. Chỉ thêm một bước model khi một thử nghiệm hữu hạn chứng minh cải thiện đủ lớn để bù latency/quota.

Retrieval contract cố định trả chunk ID, document version, nội dung và retrieval mode. Dùng metadata/full-text làm baseline; đánh giá semantic retrieval bằng các truy vấn diễn đạt khác và tiếng Việt/Anh. Chỉ bật embedding mặc định khi tăng chất lượng trong bộ truy vấn đã gán nhãn. Mất embedding không chặn incident capture hay runbook lookup. Mất inference vẫn cho phép đọc evidence, ghi chú và xử lý thủ công.

## Tham chiếu sản phẩm và khoảng trống cần bổ sung

HolmesGPT cho thấy giá trị của investigation theo câu hỏi và runbook có các bước điều tra hiển thị rõ. K8sGPT có phân tích theo resource/namespace và output có cấu trúc. Đây là bằng chứng về các workflow đã tồn tại, chưa phải bằng chứng rằng UX cụ thể của CloudOps sẽ tốt hơn. [HolmesGPT runbooks](https://holmesgpt.dev/0.23.0/reference/runbooks/), [K8sGPT](https://github.com/k8sgpt-ai/k8sgpt).

CloudOps nên thêm các khả năng gắn với công việc:

| Khoảng trống trong proposal cũ | Bổ sung cho release | Giới hạn phạm vi |
|---|---|---|
| Chỉ bắt đầu bằng alert | Investigate workload theo namespace, workload, câu hỏi và time window | Form có scope; không có terminal/chat toàn quyền |
| RCA là một kết quả tĩnh | Điều tra lại, so sánh evidence và kết luận với lần trước | So sánh trong cùng incident; không xây graph causal toàn cluster |
| Người duyệt phải tự ghép nhiều dữ kiện | Một vùng review hành động gồm lý do, bằng chứng, before/after, rủi ro và tiêu chí thành công | Preview là đánh giá điều kiện thực thi, không giả lập chắc chắn hậu quả |
| Kết thúc action bị lẫn với kết thúc incident | Ghi outcome, resolution reason, reopen và báo cáo bàn giao | Không tích hợp ticketing hoặc on-call scheduling |
| Lỗi integration làm người dùng khó hiểu | Health, telemetry freshness, lý do bị chặn và bước xử lý tiếp | Không xây màn quản trị toàn bộ provider/tool/plugin |

Cần kiểm chứng khả năng sử dụng bằng thao tác của reviewer: từ incident tìm được bằng chứng hỗ trợ kết luận, nhận biết plan không hợp lệ, và giải thích được kết quả sau action. Không dùng số trang hoặc số biểu đồ làm tiêu chí đầy đủ tính năng.

## Telemetry và detection

Prometheus/Alertmanager đảm nhiệm ngưỡng cảnh báo, grouping, inhibition và routing nền. CloudOps vẫn cần idempotency ở ingestion vì webhook có thể được giao lại; việc group alert không tự giải quyết lifecycle incident của app. [Alertmanager](https://prometheus.io/docs/alerting/latest/alertmanager/).

Mỗi evidence ghi observed time, collection time, resource UID và window. Logs hiện tại/previous, events, metrics và deployment revision phải có giới hạn kích thước; thiếu dữ liệu không được biến thành zero. Thu snapshot khi incident mở và khi re-investigate; retention có cấu hình, snapshot liên quan plan được giữ ít nhất bằng action/audit.

Threshold detection là release requirement. Median/MAD là thử nghiệm phụ trên cùng nhãn, không chặn phát hành. Chưa cần forecasting, anomaly model training hoặc một detector platform riêng.

## Remediation và mức kiểm soát cần thiết

Ba template hiện tại phù hợp một phạm vi ghi dễ giải thích: restore known-good Deployment template, đổi memory profile đã cấu hình và scale replicas trong bounds. Mỗi template phải có điều kiện áp dụng và tình huống trả manual-only; phát hiện triệu chứng không tự suy ra hành động luôn đúng.

Kubernetes hỗ trợ conditional update để phát hiện xung đột. Việc đọc precondition rồi gửi một patch vô điều kiện vẫn có cửa sổ race; executor cần đưa điều kiện vào thao tác ghi và reconcile nếu kết quả không rõ. [API concepts](https://kubernetes.io/docs/reference/using-api/api-concepts/).

Giữ approval, scope, plan hash/TTL, current-state checks, duplicate prevention, fresh post-check, audit và rollback có điều kiện. Đây là phần đúng đắn của thao tác ghi. Chưa cần policy DSL, nhiều cấp duyệt, multi-tenant isolation, HA workflow, ký số audit hay compliance platform.

Rollback không mặc định tốt hơn giữ trạng thái mới: giảm memory trở lại profile vừa OOM có thể gây tái phát. Mỗi template xác định rollback eligibility; trạng thái không an toàn/không chắc chắn chuyển manual intervention. Restore template không rollback dữ liệu hoặc side effects bên ngoài Kubernetes.

## FinOps và cửa sổ dữ liệu

Tại KRR `v1.31.0`, `history_duration` mặc định là 336 giờ; strategy simple có kiểm tra lịch sử tối thiểu ba giờ và số điểm, đồng thời mặc định tránh khuyến nghị cho metric do HPA điều khiển. Đây là điều kiện thuật toán, không phải chứng nhận workload đã được quan sát đại diện. [Settings source](https://github.com/robusta-dev/krr/blob/v1.31.0/robusta_krr/core/abstract/strategies.py), [simple strategy](https://github.com/robusta-dev/krr/blob/v1.31.0/robusta_krr/strategies/simple.py).

README mô tả memory maximum trong tuần; source mặc định 14 ngày cho thấy proposal phải chốt flags theo version thay vì suy từ mô tả tổng quát. [KRR README](https://github.com/robusta-dev/krr#algorithm).

HPA dựa trên utilization có sử dụng resource requests. Vì vậy thay requests có thể đổi hành vi scale dù tải thực không đổi. Release chỉ đưa recommendation và export, đánh dấu HPA/OOM/coverage exclusions, chưa tự apply FinOps. [HPA](https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/).

Đề xuất profile lab mặc định 6 giờ liên tục với workload phases đã định nghĩa; chỉ mở rộng đến 24 giờ khi estimate cho phép. Đây là lựa chọn thiết kế phải thử, không phải ngưỡng bảo đảm của KRR. Báo cáo ghi requested window, observed coverage, gaps, version và tải đã quan sát. Một khuyến nghị hợp lệ trên dữ liệu demo chưa chứng minh phù hợp chu kỳ tải tuần/tháng.

Giảm requests trên một EC2 cố định là giảm allocation, chưa phải giảm bill. Chi phí phân bổ phải có phương pháp, đơn vị, region và rate-card date; không cộng CPU/RAM theo cách tính trùng toàn bộ node. Nếu chưa có rate card hợp lệ, vẫn hiển thị tài nguyên và ẩn money estimate.

## Triển khai và khả thi

Compose cho ứng dụng, K3s cho lab là topology phù hợp môi trường demo nội bộ. K3s công bố tài nguyên nền không bao gồm workload; không thể dùng minimum của K3s để khẳng định cả stack đủ trên 8 GiB. Cần đo tải cùng Prometheus, database, agent và workload trước khi chốt instance. [K3s requirements](https://docs.k3s.io/installation/requirements).

EC2 dừng không xóa instance; khi start có thể đổi public IPv4. Các phiên dùng theo nhu cầu phải có startup health check, lưu dữ liệu bền vững và xác nhận trạng thái stopped sau phiên. [EC2 stop/start](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/Stop_Start.html).

Budget cũ 25–30 USD, dự phòng 40 USD là envelope kế hoạch, chưa là báo giá cho một instance đã chọn. Tổng cần gồm compute hours, EBS theo thời gian giữ, IPv4, snapshots và transfer. Khi data coverage cần lâu hơn 40–60 giờ dự kiến, thay đổi lịch chạy hoặc envelope; không cắt warm-up rồi vẫn giữ claim chất lượng.

Chuyển bảng patch version chi tiết khỏi quyết định tính năng. Chốt dependencies CloudOps bằng lockfiles, install/build và contract smoke checks thực tế; không tự nâng đồng loạt Node/Python/PostgreSQL vì có bản mới. Không còn yêu cầu tương thích runtime upstream. Research này không xác nhận toàn bộ version trong proposal cũ.

## Đánh giá đủ cho release

AIOpsLab tách detection, localization, analysis và mitigation; RCAEval cung cấp telemetry có nhãn và baseline. Áp dụng cách tách nhiệm vụ và kiểm soát ground truth, không đưa cả benchmark suite vào đường bắt buộc của app. [AIOpsLab](https://github.com/microsoft/AIOpsLab), [RCAEval](https://github.com/phamquiluan/RCAEval).

Đề xuất bộ nghiệm thu gồm 15 lượt live recovery có reset độc lập cho ba scenario; bốn diagnostic cases cho Pending và ImagePull; năm negative cases; và bộ contract tests tập trung vào quyền, stale/duplicate và write-uncertain. Fixture và live results phải ghi riêng. Ground truth chỉ đi vào evaluator, không vào agent context.

Đo supported cause/category, evidence relevance, abstention, plan eligibility, action outcome và thời gian từng bước. Với 15 lượt không diễn giải P95 như một cam kết ổn định; công bố toàn bộ quan sát, median/range và số mẫu. Mốc 12/15 là mục tiêu chất lượng nội bộ ban đầu, không thay thế xử lý từng lỗi có nguy cơ ghi sai.

So sánh tối thiểu rule baseline với Investigator dùng evidence và full-text runbook trên cùng dữ liệu. Nhánh semantic retrieval là ablation mở rộng tùy chọn, không chặn release nếu bỏ semantic. Mục tiêu là xác định phần nào đáng giữ, không tổ chức một cuộc thi model lớn. UX nghiệm thu bằng hai luồng alert/manual và một lần bàn giao cho người khác.

## Thử nghiệm tích hợp trước khi triển khai rộng

| Thử nghiệm | Bằng chứng cần thu | Quyết định nếu không đạt |
|---|---|---|
| Direct workflow và endpoint | Collector đăng ký evidence; worker chuẩn bị context, gọi model, validate RCAResult và persist stage | Sửa collector/context/client/job implementation; không tự quay lại runtime ngoài |
| Structured RCA | Valid/invalid/truncated/unknown-citation outputs không bị formatter thay đổi kết quả máy đọc | Typed result seam riêng; invalid output chỉ là analysis failure |
| Scope và job recovery | Collection pack chỉ đọc; namespace ngoài scope bị từ chối; restart/lease hết hạn không làm mất run hoặc ghi đè result mới | Sửa scope filter và job claim/checkpoint; model không nhận tool/cluster credential |
| Embedding | Hai chunk ra hai vector riêng, đúng dimension; truy vấn có nhãn; cache và fallback | Giữ full-text nếu chất lượng hoặc khả năng chạy không đạt |
| OOM flow | Plan, approve/reject, duplicate, stale, fresh verification và manual fallback | Sửa contract/eligibility trước khi bật action khác |
| Full stack và KRR | CPU/RAM/disk, data coverage, restart/restore trong một phiên đại diện | Điều chỉnh instance/window/budget theo số đo |

Các thử nghiệm này xác nhận implementation. Định hướng tính năng có thể chốt trước, với tính năng bị chặn hiển thị rõ cho đến khi integration tương ứng đạt.

## Các chỉnh sửa chính so với proposal ban đầu

| Mục cũ | Chỉnh sửa đề xuất |
|---|---|
| Mục tiêu và bảng MVP | Đổi thành phạm vi release nội bộ; mô tả workflow hoàn chỉnh và giới hạn áp dụng |
| Next.js và BFF | React/Vite frontend riêng, reverse proxy cùng origin; CloudOps API là backend sản phẩm |
| Hai role LLM | Một Investigator profile cố định và deterministic plan builder |
| Akmatori proposals | Chỉ tham khảo; action approval/execution là domain riêng |
| API POST read tools | Worker retrieval nội bộ theo run scope; search cho browser có session/scope checks, không có model tool connector |
| Incident state machine | Tách lifecycle incident, investigation run và action lifecycle |
| Dashboard năm trang | Thiết kế theo công việc; thêm manual/reinvestigation, health và handoff; tránh clone navigation upstream |
| Semantic RAG bắt buộc | Retrieval bắt buộc, vector search có điều kiện theo thử nghiệm |
| Slack approval bắt buộc | Web approval đầy đủ; Slack notifications là tiện ích sau luồng chính |
| FinOps savings | Resource recommendations và allocation estimate; không nhận là bill savings |
| Version và timeline cố định | Build-verified baseline; milestones theo bằng chứng, rà lại lịch từ tiến độ thực |
| Hardening | Giữ kiểm soát gắn với ba action; hoãn yêu cầu doanh nghiệp không có use case hiện tại |

Proposal cuối phải được phản biện về sự nhất quán giữa scope, UX, contracts và acceptance. Việc review tài liệu không thay thế kiểm thử runtime hoặc nghiệm thu người dùng.
