# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Mỗi case được xử lý độc lập. CLI phát `case_received`, sau đó coordinator phân công
các specialist thu thập dữ liệu có thẩm quyền qua MCP. Workflow dùng kết quả
`tools/list`, bao gồm input schema, để chọn tool và chỉ truyền những ID đã biết. Evidence
được chuyển cho verifier để đối chiếu claim, timeline, tiền và policy. CLI validate
output theo public contract trước khi ghi file và phát `case_finalized`.

```text
Input → Coordinator → Specialists → Verifier → Output
                         │              │
                         └── MCP ───────┴── Trace
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | Input case, discovered tool specs | Phân công task theo domain và giữ `case_id` là correlation key | Task envelope cho specialist |
| Order/item | Claimed order ID, order/item tool schema | Xác minh order status, item, seller, giá, freight và shipping limit | Order/item evidence và entity IDs cho verifier |
| Payment | Order ID, payment/refund tool schema | Đối soát capture, split payment, duplicate, refund status và số tiền | Payment evidence và payment references cho verifier |
| Shipment | Order/shipment ID, shipment tool schema | So sánh handoff, estimated delivery và actual delivery | Shipment evidence và timeline verdict input |
| Seller | Seller ID từ item evidence | Bổ sung seller identity/SLA khi tool profile cung cấp | Seller evidence cho verifier |
| Policy | `policy_version`, claim topic | Lấy policy áp dụng; không tự đặt eligibility hay action | Policy evidence và observable `policy_decided` |
| Verifier | Tất cả specialist evidence | Phân loại issue, giải quyết conflict, tính refund và kiểm tra invariant | Output V2 cho coordinator |

Quyền gọi tool được giới hạn bằng domain trong tên/mô tả tool: order/item agent chỉ
dùng order và item; payment agent chỉ dùng payment/refund; shipment agent chỉ dùng
shipment/delivery/logistics; seller agent chỉ dùng seller; policy agent chỉ dùng policy.

## 3. A2A protocol

Logical message envelope gồm `case_id`, `actor`, `target`, `decision_code`, domain IDs và
`evidence_refs`; raw reasoning không nằm trong message hoặc trace. Coordinator phát một
`task_assigned` cho mỗi specialist. Specialist chỉ handoff sau khi tool trả về hoặc sau
một lỗi có quan sát được. Mỗi domain được ghé thăm tối đa một lần theo thứ tự
`order → item → payment → shipment → seller → policy`, nên không có handoff loop.

Timeout network do gateway giới hạn (30 giây connect/write/pool, 300 giây tổng). Workflow
không retry call có thể đã được audit; lỗi chuyển thành `MCP_TOOL_FAILED` và verifier
chỉ dùng evidence đã validate thành công.

## 4. Evidence lifecycle

`EvidenceGateway.call` luôn thêm `case_id` và validate envelope với
`mcp-evidence-response-v1` trước khi trả dữ liệu. Workflow lưu tạm bộ bốn
`(domain, tool_name, evidence_ref, data)` trong phạm vi một lần `solve_case`; bộ này không
được cache qua case.

Khi specialist dùng response, workflow phát `tool_result_consumed` với chính ref server
trả về. Verifier chỉ gắn ref thuộc domain hỗ trợ claim: order/payment/policy cho
payment-refund, và order/item/shipment/seller/policy cho delay. Top-level `evidence_refs` là
hợp có thứ tự, loại trùng, của các ref đã thực sự dùng. Workflow không sửa hay
tự sinh `evidence_ref`.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout/network error | Không retry call đã audit | Tiếp tục với evidence domain khác; nếu không đủ thì `insufficient_evidence` | `handoff/MCP_TOOL_FAILED` |
| Entity/tool not found | Không | Không tự đoán ID; handoff không có evidence | `handoff/NO_EVIDENCE_AVAILABLE` |
| Source conflict | Không | Giữ conflict có cấu trúc; chọn authoritative source nếu MCP cung cấp resolution | `verification_completed` và `data_conflicts` |
| Invalid MCP envelope | Không | Gateway loại response; verifier không được dùng ref/data đó | `handoff/MCP_TOOL_FAILED` |
| Invalid specialist result | Không | Verifier tái tạo conclusion từ evidence records hợp lệ | `verification_completed` |

Không chuyển missing evidence thành dữ liệu phỏng đoán. Fallback
`insufficient_evidence` có confidence thấp và action `MANUAL_INVESTIGATION`.

## 6. Verification invariants

Trước handoff về coordinator, verifier áp dụng các invariant sau:

- `case_id` output bằng input; mọi MCP call cùng `case_id`.
- Entity trong output chỉ lấy từ input hoặc MCP evidence của case hiện tại.
- Mọi submitted evidence ref đều đến từ envelope đã qua schema validation và được
  gắn vào domain hỗ trợ conclusion/claim.
- `recommended_refund_brl >= 0`; tổng `refund_lines` bằng recommended refund; tiền được
  làm tròn hai chữ số.
- `no_action` chỉ dùng cho valid split hoặc unsupported claim;
  `needs_investigation` chỉ dùng khi thiếu evidence.
- Seller responsibility chỉ kèm seller ID đã quan sát; các party khác dùng `null` thay
  vì tự tạo ID.
- Action lấy từ policy evidence nếu policy trả một tập hẹp theo case; nếu không thì
  dùng action code xác định theo issue, không trùng lặp.
- Confidence nằm trong `[0, 1]` và giảm xuống `0.25` khi không có evidence.
- CLI validate output theo `l3a-output-v2` trước khi ghi file.

## 7. Reproducibility

Workflow là deterministic rule-based Python 3.11+, không gọi model ngoài, không có
temperature hay random seed. Dependencies được giới hạn version trong `pyproject.toml`.
CLI xử lý tuần tự 100 case (concurrency limit 1), và MCP tool specs được cache trong
một run. Các lệnh tái lập:

```bash
day09 validate-inputs
day09 mcp-tools
day09 run
day09 validate
day09 package --output dist/submission.zip
```

Runtime config chỉ gồm ba biến trong `.env`: competition URL, team API key và MCP endpoint.
Tài liệu, trace và submission không ghi API key.
